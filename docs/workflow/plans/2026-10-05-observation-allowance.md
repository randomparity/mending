# Implement the repair-cycle observation allowance

Goal: reconcile a recorded attempt under a bounded, read-only observation allowance,
record late facts with the failure retained, and require an operator disposition
before new work. Architecture: two config keys and three state fields on the existing
dataclasses in `desloppify/engine/repair_cycle.py`; reconcile, receipt acceptance,
and a dispose path in `desloppify/app/commands/repair_cycle.py`; one parser flag.
Spec: `docs/workflow/specs/2026-10-05-observation-allowance-design.md`.
Stack: Python 3.11+, stdlib only, pytest via `uv`.

## Global Constraints

- No new dependency. Do not touch `desloppify/engine/repair_queue.py`,
  `desloppify/app/commands/repair_queue.py`, or `desloppify/engine/repair_manifest.py`.
- Execution calls (`verify_authority`, `select`) keep `_call_before_deadline`.
- Persist (`_persist_before_external_call`) before every adapter call, under the lock.
- Legacy state (no new keys) must decode; never drop a recorded lease.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`,
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

Expected implementation size: 220–300 changed lines (M) — ~60 engine lines, ~70 command
lines, ~5 parser lines, ~10 doc lines, ~120 test lines.

## File map

- `desloppify/engine/repair_cycle.py` — owns config and state decoding; gains
  `observation_call_limit`, `observation_seconds`, `observation_calls`,
  `attempt_failure`, `disposed_attempt`, `awaiting_disposition`, `fail`, `dispose`.
- `desloppify/app/commands/repair_cycle.py` — owns the run flow; reconcile moves to the
  observation bound, receipt acceptance records late facts, new dispose path.
- `desloppify/app/cli_support/parser_groups_repair_cycle.py` — `--dispose-attempt`.
- `docs/systemd/repair-cycle.md` — config keys and overrun/disposition wording.
- `desloppify/tests/commands/test_repair_cycle.py` — focused tests.

## Task 1: Config and state

Interfaces: produces `CycleConfig.observation_call_limit: int` (default 3),
`CycleConfig.observation_seconds: int` (from `observation_minutes`, default 5 → 300),
`CycleState.observation_calls: int`, `CycleState.attempt_failure: str | None`,
`CycleState.disposed_attempt: str | None`, property `CycleState.awaiting_disposition -> bool`,
`CycleState.fail(reason: str) -> None` (sets `attempt_failure` only when None, then parks),
`CycleState.dispose(attempt_id: str) -> None` (raises `ValueError` when no current lease
has that ID or no failure is recorded).

Steps: add the config keys via existing `_positive_int`; add state fields with defaults,
decode them in `from_mapping` (count: non-bool int ≥ 0, default 0; the two strings:
`str | None`), encode in `to_mapping`; `begin` resets the three fields; change
`_may_replace_lease` to: same-day → False; awaiting disposition → False; disposed
current attempt → True; else terminal receipt.

Verification:
- Mode: focused-test — `test_config_observation_keys_default_and_decode`: defaults
  3/300; `observation_minutes=2` → 120; `observation_call_limit=0` raises. Red:
  `AttributeError`. Green: `uv run --locked pytest desloppify/tests/commands/test_repair_cycle.py -q`.
- Mode: focused-test — `test_legacy_state_decodes_and_new_fields_validate`: the
  mapping from a pre-change `to_mapping()` minus the new keys decodes with 0/None/None;
  `observation_calls=-1`, `True`, and `attempt_failure=3` raise `ValueError`. Red:
  missing attribute. Green: same command.

## Task 2: Observation-bounded reconcile and late facts

Interfaces: consumes Task 1. Produces `_call_within(seconds: float, operation)`;
`_call_before_deadline(args, lease, operation)` delegates to it with the remaining lease
time and still raises `TimeoutError` when none remains.

Steps: in `cmd_repair_cycle`, with a current lease: terminal or disposed →
`_begin_and_select`; otherwise `_reconcile`, then `_begin_and_select` only when it
returns True. `_begin_and_select` parks `disposition-required` before `begin` while
`awaiting_disposition`. `_reconcile`: if `observation_calls >=
config.observation_call_limit` → `fail("observation-exhausted")` and park
`observation-exhausted`, no call; else increment, store, persist, and call
`client.reconcile` through `_call_within(config.observation_seconds, ...)`;
`TimeoutError` → park `observation-timeout`, other exceptions → park
`reconciliation-unavailable`; return accepted-and-terminal. `_accept_receipt`: after
shape/merge checks and the `unknown` check, record the receipt (consume merge permit
when reported), then `_limit_failure(lease, receipt, now)` returns `runtime-exhausted`
(`now > deadline`) or `budget-exhausted` (calls/cost over the lease); on a failure call
`fail(reason)`, store, return False. Remove both limit checks from `_receipt_reason`.

Verification:
- Mode: focused-test — `test_expired_lease_is_observed_without_execution` replaces
  `test_expired_lease_does_not_make_another_adapter_call`: lease at NOW with 1 minute,
  run at NOW+2m; `reconciliations == 1`, `verifications == selections == 0`, receipt
  recorded, `attempt_failure == "runtime-exhausted"`, parked `runtime-exhausted`.
  Red: `reconciliations == 0`. Green: focused command.
- Mode: focused-test — `test_observation_allowance_is_bounded_and_persisted`: limit 1,
  reconcile raises; run 1 parks `reconciliation-unavailable` with `observation_calls == 1`
  persisted in the state file before the call (file-backed, asserted inside the client);
  run 2 makes no call and records `observation-exhausted`. Red: count not persisted.
- Mode: focused-test — `test_observation_call_is_time_bounded` monkeypatches
  `signal.setitimer` as the existing deadline-timer test does; parks
  `observation-timeout` with an expired lease. Red: parks `timeout` today.
- Mode: focused-test — rename `test_timeout_and_budget_exhaustion_park_without_accepting_receipt`
  to `test_execution_timeout_parks_and_budget_overrun_records_usage`: the timeout half
  keeps `parked_reason == "timeout"`, no receipt, `attempt_failure is None`; the budget
  half asserts the receipt is recorded with `attempt_failure == "budget-exhausted"`.
  Rename `test_overdue_adapter_result_parks_before_receipt_is_accepted` to
  `test_overdue_adapter_result_is_recorded_as_runtime_failure` and assert the receipt
  is recorded with `attempt_failure == "runtime-exhausted"`. Red: receipt is None.

## Task 3: Disposition gate and dispose flag

Interfaces: consumes Task 1 `dispose`, `awaiting_disposition`. Produces parser flag
`--dispose-attempt ATTEMPT_ID`, read as `getattr(args, "dispose_attempt", None)`.

Steps: in `cmd_repair_cycle`, inside the lock and before any adapter call, when
`dispose_attempt` is set call `dispose`, store, persist, print
`Repair cycle attempt <id> disposed.`; map `ValueError` to `CommandError(..., exit_code=2)`.
Add the parser argument. Update `docs/systemd/repair-cycle.md`: the two config keys
with defaults, and replace "A deadline overrun parks the recorded attempt" with the
observation, failure-retention, and `--dispose-attempt` behavior, including that any
attempt first observed after its deadline needs a disposition.

Verification:
- Mode: focused-test — `test_failed_attempt_requires_disposition_before_new_work`: after
  the expired-lease run, a next-day run parks `disposition-required` with zero new calls;
  `--dispose-attempt` with a wrong ID raises `CommandError`; with the right ID records it;
  the next-day run selects once with a new lease and cleared failure. Red: next-day run
  selects without disposition.
- Mode: focused-test — `test_parser_wires_dispose_attempt`. Red: unrecognized argument.
- Mode: task-test-not-applicable — `docs/systemd/repair-cycle.md`: operator prose; no
  executable consumer validates it.
