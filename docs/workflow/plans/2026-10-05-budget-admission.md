# Implement repair-cycle budget admission

Goal: admit each host dispatch against the lease's remaining budget, enforce it
during host work through the Claude Code adapter, settle it to measured usage,
and persist consumed and reserved budget across interruption. Architecture: four
`CycleState` fields plus `admit`/`settle` in `desloppify/engine/repair_cycle.py`;
budget flags, stream measurement, a call-limit stop, and a capability check in
`desloppify/app/commands/repair_cycle_host.py`; one dispatch seam in
`desloppify/app/commands/repair_cycle.py`.
Spec: `docs/workflow/specs/2026-10-05-budget-admission-design.md`.
Stack: Python 3.11+, stdlib only, pytest via `uv`.

## Global Constraints

- No new dependency. Do not touch `desloppify/engine/repair_queue.py`,
  `desloppify/app/commands/repair_queue.py`, or `desloppify/engine/repair_manifest.py`.
- Do not change `cmd_repair_cycle`'s flow or the receipt path (#31 owns both).
- Persist (`_persist_before_external_call`) before the adapter launches, under the lock.
- Legacy state (no new keys) must decode; never drop a recorded lease or reservation.
- No ADR (none assigned).
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`,
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

Expected implementation size: 230–310 changed lines (M) — ~60 engine lines, ~90 adapter
lines, ~25 seam lines, ~120 test lines.

## File map

- `desloppify/engine/repair_cycle.py` — owns persisted attempt state; gains
  `BudgetAdmission`, the four budget fields, `admit`, `settle`, `budget_exceeded`.
- `desloppify/app/commands/repair_cycle_host.py` — owns the host process; `run`
  takes a `BudgetAdmission`, passes the budget flags, measures calls and cost from
  the event stream, stops on the call limit, parks `unenforceable-limit`.
- `desloppify/app/commands/repair_cycle.py` — gains `_dispatch_host`, the
  admit-persist-run-settle seam #31 composes; #31 builds the request from the
  current lease (attempt ID, deadline).
- `desloppify/tests/commands/test_repair_cycle.py`,
  `desloppify/tests/commands/test_repair_cycle_host.py` — focused tests.

## Task 1: Budget state

Interfaces: produces `BudgetAdmission(cost_usd: Decimal, calls: int)` (frozen
dataclass), `CycleState.consumed_cost_usd: Decimal`, `consumed_calls: int`,
`reserved_cost_usd: Decimal`, `reserved_calls: int`,
`CycleState.admit() -> BudgetAdmission | None`,
`CycleState.settle(admission: BudgetAdmission, cost_usd: Decimal | None, calls: int) -> None`,
`CycleState.budget_exceeded: bool` (property).

Verification:
- Contract: admission reserves the remainder and refuses an exhausted lease.
  Mode: focused-test. `test_budget_admission_reserves_remainder_and_settles` —
  red: `AttributeError: 'CycleState' object has no attribute 'admit'`.
- Contract: settlement charges measured usage, or the whole admission when cost
  is unmeasured. Mode: focused-test. Same test plus
  `test_unmeasured_cost_charges_whole_admission`.
- Contract: the four fields round-trip, default to zero in legacy state, reject
  negative/boolean/non-numeric values, and reset in `begin`. Mode: focused-test.
  Extend `test_legacy_state_decodes_and_new_fields_validate` — red: keys absent
  from `to_mapping()`.
- Green: `uv run --locked pytest -q desloppify/tests/commands/test_repair_cycle.py`.

Steps:
1. Write the tests above; run and confirm red.
2. Add `BudgetAdmission` and the fields; decode costs with a non-negative
   `Decimal` helper (string or number, finite, `>= 0`) and calls with a
   non-negative non-bool int check; serialize costs with `str`.
3. `admit`: raise `ValueError("no current lease")` without a lease; compute
   `cap - consumed - reserved` for cost and calls; return `None` if either is
   `<= 0`; else add both to the reservation and return them.
4. `settle`: subtract the admission from the reservation, add `calls`, add
   `cost_usd` or, when `None`, `admission.cost_usd`.
5. `budget_exceeded`: lease present and consumed cost `>` cap or consumed calls
   `>` call limit. Reset all four in `begin`. Export `BudgetAdmission`.
6. Run green; commit `feat(repair-cycle): persist admitted and consumed budget`.

## Task 2: Adapter limits and measurement

Interfaces: consumes `BudgetAdmission`. Produces
`ClaudeHostAdapter.run(request: HostRequest, admission: BudgetAdmission) -> HostOutcome`;
`HostOutcome.cost_usd: Decimal | None = None`, `HostOutcome.calls: int = 0`.

Verification:
- Contract: argv carries `--output-format stream-json --verbose
  --forward-subagent-text --max-budget-usd <admission cost>`, and the host
  environment carries `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. Mode: focused-test.
  Update `test_completed_run_reports_identity` — red: `TypeError` for the new
  `admission` argument, then argv lacks the flags.
- Contract: measured cost and distinct-message call count reach the outcome,
  counting subagent events (`parent_tool_use_id` set) and collapsing events
  that share a `message.id`; an `error_during_execution` result yields
  `cost_usd is None`. Mode: focused-test. `test_stream_usage_is_measured`.
- Contract: a host exceeding admitted calls is stopped (`stopped`/`call-limit`,
  tree gone). Mode: focused-test. `test_call_limit_stops_whole_tree` — red:
  outcome is `stopped`/`timeout` once the signature accepts an admission.
- Contract: missing help option, failing help, a version below 2.1.275, or no
  `/proc` parks `unenforceable-limit` with no launch. Mode: focused-test.
  `test_unenforceable_limit_parks_without_launch` (parametrized; `/proc`
  absence injected by monkeypatching a module-level `_PROC_ROOT`).
- Contract: each test request uses a fresh attempt ID. Mode:
  task-test-not-applicable — the change is to test fixtures themselves; the
  concurrent-run collision it removes is not reproducible inside one pytest run.
- Green: `uv run --locked pytest -q desloppify/tests/commands/test_repair_cycle_host.py`.

Steps:
1. Stand-in host: answer `--version` with `FAKE_HOST_VERSION` (default
   `2.1.289 (Claude Code)`) and `--help` with the three options unless
   `FAKE_HOST_HELP=bare` (omit `--max-budget-usd`) or `fail` (exit 1). Rewrite
   every existing mode's output as stream-json lines: two `assistant` events
   sharing one `message.id`, one subagent `assistant` event with
   `parent_tool_use_id`, then a `result` event (`is_error`, `subtype`,
   `total_cost_usd`); `fail` emits an error result, `garbage` stays non-JSON.
   Mode `chatty` emits assistant events with fresh IDs every 50 ms until killed;
   mode `crash` emits an `error_during_execution` result. `_request` uses
   `uuid.uuid4().hex`; session assertions derive from `request.attempt_id`.
   Every existing `.run(_request(...))` call passes a `BudgetAdmission`;
   `test_missing_proc_is_unknown` keeps patching `_marked_pids` (post-launch).
2. Write the tests above; run and confirm red.
3. `_preflight` gains the capability check after the skills checks:
   `_PROC_ROOT` (also used by `_marked_pids`) not a directory; or
   `subprocess.run([exe, flag], capture_output=True, text=True, timeout=10)`
   for `--version` and `--help` raising `OSError`/`TimeoutExpired` or exiting
   nonzero; the leading `N.N.N` of `--version` below `(2, 1, 275)` or absent; or
   `--help` missing a required option → `HostOutcome("parked",
   "unenforceable-limit")`.
4. Add a `_StreamUsage` reader: buffers partial lines, counts distinct
   assistant `message.id` values (plus one per assistant event without an ID),
   keeps the last `result` event; `cost_usd` property per the spec.
5. Replace `proc.wait` with a poll loop that feeds the reader from the stdout
   file each `_POLL_SECONDS` and returns stop reason `call-limit` once
   `calls > admission.calls`, `timeout` at the deadline, or `None` on exit;
   then `_stop_tree` as before and feed the rest. The launch environment adds
   `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` beside the session marker.
6. Map outcomes: stop reason with empty tree → `stopped`/reason; with survivors
   → `unknown`/`<reason>-survivors`; no stop and survivors → `unknown`/
   `worker-survivors`; else the result event decides `completed`/`failed`, no
   event → `invalid-host-output`. Attach `cost_usd` and `calls` to every
   launched outcome.
7. Run green; commit `feat(repair-cycle-host): admit and measure host budgets`.

## Task 3: Dispatch seam

Interfaces: consumes Tasks 1 and 2. Produces
`_dispatch_host(args, state, cycle_state, adapter, request) -> HostOutcome | None`
in `desloppify/app/commands/repair_cycle.py`.

Verification:
- Contract: the reservation is on disk before the adapter runs and settled
  after. Mode: focused-test. `test_dispatch_persists_reservation_before_launch`:
  run under `state_lock` on a `tmp_path` state file with `state_data=None`
  (pattern of `test_lease_is_persisted_before_authority_verification`); the fake
  adapter reads `json.loads(state_path.read_text())` — red: `AttributeError`.
  Bite check: remove the persist call and observe red.
- Contract: past the deadline fails `runtime-exhausted` without admitting; an
  exhausted lease fails `budget-exhausted` without running; an over-budget
  outcome fails `budget-exhausted`; a parked outcome settles at zero and parks
  with its reason; `stopped`/`unknown` settle the whole admission; a mismatched
  attempt ID or later deadline raises `ValueError`. Mode: focused-test.
  `test_dispatch_budget_outcomes` (parametrized).
- Contract: an interrupted dispatch keeps its reservation on disk, and the next
  admission fails `unsettled-reservation`. Mode: focused-test.
  `test_interrupted_dispatch_keeps_reservation`: the fake adapter raises
  `KeyboardInterrupt`; a fresh `CycleState` decoded from the file is used next.
- Green: `uv run --locked pytest -q desloppify/tests/commands/test_repair_cycle.py`.

Steps:
1. Write the tests; confirm red.
2. Implement per the spec's "Dispatch seam" section.
3. Run green, then every guardrail in Global Constraints; commit
   `feat(repair-cycle): admit, persist, and settle each host dispatch`.
