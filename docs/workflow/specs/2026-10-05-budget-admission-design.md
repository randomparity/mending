# Repair-cycle budget admission design

Issue #29, part of #19. No new decision record: ADR 0010 already chose the Claude
Code host partly for `--max-budget-usd` and states that budget admission extends
its adapter; ADR 0007 keeps the persisted lease and its budgets as Mending's
attempt record. This change makes those budgets admitted, measured, and durable.

## Problem

Calls and cost are compared to the lease only when a self-reported receipt
arrives (`_limit_failure` in `app/commands/repair_cycle.py`), so a host can
overspend first. The host adapter passes no limit to the host and reports no
usage. `CycleState` keeps no consumed or reserved budget, so an interrupted
dispatch leaves no record of what it may have spent.

## Scope

No ownership transition: a clean extension of `CycleState`
(`desloppify/engine/repair_cycle.py`), `ClaudeHostAdapter`
(`desloppify/app/commands/repair_cycle_host.py`), and one dispatch seam in
`desloppify/app/commands/repair_cycle.py`. Composing that seam into
`cmd_repair_cycle` is #31 (ADR 0010, Consequences); until then the receipt path
and its after-the-fact `_limit_failure` check stay as they are, and #31 removes
them with the `AdeptCycleClient`.

### Limits and who enforces them

| Limit | Admission | Enforcement during work | Measurement |
|---|---|---|---|
| USD cost | remaining lease cost passed as `--max-budget-usd` | the host stops the session (subagents included) | result event `total_cost_usd` |
| Model calls | remaining lease calls | Mending counts calls in the host's event stream and stops the worker tree once the count exceeds the admission | distinct assistant `message.id` values, main and subagent |
| Runtime | lease deadline (existing) | Mending stops the worker tree at the deadline (existing) | wall clock |

The host runs with `--output-format stream-json --verbose
--forward-subagent-text`. Per the Claude Code headless docs ("Follow subagent
messages"), a foreground subagent's `tool_use` blocks are streamed by default and
the flag adds its text and thinking blocks, as `assistant` events carrying
`parent_tool_use_id`, at every nesting depth from v2.1.275; each completed
content block is its own event and blocks of one response share a `message.id`.
So distinct IDs count one per model response; an assistant event without a
`message.id` counts as one call. Mending makes no
retries; review and revalidation run inside the host session, so they are
covered by the same session limits. Every dispatch under one lease draws from
that lease's remaining budget.

### Capability check (adapter preflight)

After the existing skills checks and before launch, the adapter parks with
reason `unenforceable-limit` when `/proc` is absent (the tree stop cannot be
verified, so runtime and call stops cannot be enforced), when
`<host> --version` reports a version below 2.1.275 or cannot be parsed, or when
`<host> --help` lacks any of `--max-budget-usd`, `stream-json`,
`--forward-subagent-text`. Each probe is bounded to 10 seconds and makes no
model call; a probe that fails, exits nonzero, or times out parks the same way.

### State (`CycleState`, all absent in legacy state)

- `consumed_cost_usd: Decimal` and `consumed_calls: int`, default 0.
- `reserved_cost_usd: Decimal` and `reserved_calls: int`, default 0.
- Decoding rejects negative, boolean, or non-numeric values; cost is stored as
  a string. `begin` resets all four with the new lease.
- `admit() -> BudgetAdmission | None` reserves the whole remainder
  (`lease cap - consumed - reserved`) and returns it, or returns `None` when
  either remainder is `<= 0`. It raises `ValueError` without a current lease.
- `settle(admission, cost_usd, calls)` removes that admission's reservation and
  adds the measured calls and cost; `cost_usd=None` (unmeasured) charges the
  whole admitted cost.
- `budget_exceeded` is true when consumed cost or calls exceed the lease.
- `admit` returning `None` while a reservation is outstanding means an earlier
  dispatch was interrupted; the seam reports that as `unsettled-reservation`.

### Dispatch seam (`_dispatch_host`)

Under the caller's held state lock, with the current lease:
1. A request whose `attempt_id` differs from the lease's, or whose `deadline`
   is later than the lease's, raises `ValueError` (caller defect).
2. At or past the lease deadline, record attempt failure `runtime-exhausted`
   (as `_limit_failure` does) and return without admitting.
3. `admit`; on `None`, record attempt failure `unsettled-reservation` when a
   reservation is outstanding, else `budget-exhausted`, and return.
4. Store and persist the reservation (`_persist_before_external_call`), then
   run the adapter with the admission.
5. Settle: `parked` at zero cost and observed calls; `stopped` or `unknown`
   (Mending stopped the tree or could not verify it empty, so in-flight or
   surviving work may have spent) at unmeasured cost and
   `max(observed calls, admitted calls)`; any other outcome at its measured
   usage.
6. `parked` parks with its reason; otherwise `budget_exceeded` records
   `budget-exhausted`. Other outcomes return to the caller (#31) after the
   settled state is stored.

### Adapter outcome

`HostOutcome` gains `cost_usd: Decimal | None` and `calls: int`. Cost is
measured only from a result event whose `total_cost_usd` is a finite,
non-negative number and whose subtype is not `error_during_execution` (a crash
result may carry zeroed cost); otherwise it is `None`. A call-limit stop is
`stopped`/`call-limit` when the tree is verified empty, else
`unknown`/`call-limit-survivors`. A run with no result event is
`invalid-host-output`. The host's own budget stop arrives as an error result and
maps to `failed`/`host-error` with its measured cost; the seam then records
`budget-exhausted` because cost exceeds the lease.

### Test isolation (operator addition)

Each adapter test request uses a fresh `uuid4` attempt ID, so the derived
`MENDING_HOST_SESSION` marker differs between concurrent test runs.

## Failure model

1. Actors and deployments: the systemd timer or a local operator running the
   one-shot command for one repository state file on Linux; the Claude Code CLI
   under the host account is the external party.
2. Invariants and assets: spend per lease stays within the cost cap as enforced
   by the host, plus the overshoot below; calls stop within the overshoot below;
   a reservation is on disk before the host launches; a restarted cycle never
   admits budget an interrupted dispatch may have spent; legacy state decodes.
3. Accepted failure classes:
   - the host stops on cost only after the call that crosses the cap, and
     Mending sees a call only after it returns, so each may exceed its limit by
     the calls already in flight (bounded by host concurrency; recorded as
     consumed, then failed `budget-exhausted`); a stop by Mending charges the
     whole admission instead of guessing the in-flight count;
   - model calls the host makes that never appear as `assistant` events
     (host-internal auxiliary or compaction calls, background subagents) are
     not counted; the host's cost cap still binds them; checking the stream
     shape against a captured real transcript is the live pilot's (#7);
   - if Mending is killed uncatchably (SIGKILL), the host keeps running with
     only its own cost cap until it exits; under the systemd unit the control
     group kill ends it, and a local operator stops it before disposition;
   - an interrupted dispatch leaves its whole admission reserved, so that lease
     admits nothing further and the attempt needs observation and disposition
     (#28 path); no settlement is guessed;
   - the help-text probe proves an option exists, not that it binds under the
     deployment's auth mode (#32).
4. Covered elsewhere: whether `--max-budget-usd` binds under deployment auth and
   the host-versus-Mending report (#32); dispatch records and duplicate
   prevention (#30); composing the seam, removing the receipt budget check, and
   the outcome-state mapping (#31); observation after expiry (#28).

## Success

- A dispatch is launched only with a persisted reservation and with
  `--max-budget-usd` equal to the remaining lease cost.
- A host exceeding its admitted calls is stopped and its usage settled; the
  attempt fails `budget-exhausted`.
- A host whose `--help` lacks a required option, or a host without `/proc`,
  parks `unenforceable-limit` with no launch.
- After an interrupted dispatch, the next admission under that lease is refused
  with `unsettled-reservation`.
- Legacy state without the four fields decodes with zeros.

## Validation

Focused tests: `desloppify/tests/commands/test_repair_cycle.py` (state,
admission, settlement, seam) and `test_repair_cycle_host.py` (flags, stream
measurement, call-limit stop, capability parks, unique attempt IDs).
Guardrails: `make lint typecheck arch ci-contracts tests tests-full
package-smoke`.
