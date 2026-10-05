# Repair-cycle observation allowance design

Issue #28, part of #19. No new decision record: ADR 0007 keeps the persisted
attempt lease and its budgets as Mending's attempt record; this change only
separates reading an attempt's outcome from permission to execute under it.

## Problem

`_reconcile` runs through `_call_before_deadline`, so an expired lease parks as
`timeout` before any read and the attempt stays stranded. A receipt observed after
the deadline, or over budget, parks without being recorded, so late facts and usage
are lost. Nothing requires an operator to acknowledge a failed attempt before new
work.

## Scope

No ownership transition: a clean extension of `CycleConfig`/`CycleState`
(`desloppify/engine/repair_cycle.py`) and `cmd_repair_cycle`
(`desloppify/app/commands/repair_cycle.py`), plus one parser flag and the
config/overrun wording in `docs/systemd/repair-cycle.md`.

### Config

- `observation_call_limit`: positive int, default 3. Reconcile calls allowed per
  attempt, whether or not its lease has expired.
- `observation_minutes`: positive int, default 5. Wall-clock bound on each
  reconcile call, enforced with the existing `SIGALRM` timer.

### State (`CycleState`, all absent in legacy state)

- `observation_calls: int` (default 0): reconcile calls started for the current
  attempt. Incremented and saved before each call.
- `attempt_failure: str | None`: the first failure of the current attempt
  (`runtime-exhausted`, `budget-exhausted`, `observation-exhausted`). Never cleared
  by later receipts or parks.
- `disposed_attempt: str | None`: attempt ID the operator disposed.
- `begin` resets all three. Decoding rejects a negative, boolean, or non-int count
  and non-string failure/disposition. Legacy state decodes with the defaults, so an
  unresolved legacy lease is observed, never discarded.
- `awaiting_disposition` is true when `attempt_failure` is set and
  `disposed_attempt` differs from the current attempt ID. `begin` refuses while it
  is true; once disposed, `begin` may replace the lease on a later day even without
  a terminal receipt. The same-day rule is unchanged.

### Behavior

1. A run with a current lease that is neither disposed nor terminal observes it:
   if `observation_calls >= observation_call_limit`, record
   `observation-exhausted` and park with no call; otherwise increment, save, and
   call `reconcile` bounded by `observation_minutes`. Timeout parks
   `observation-timeout`; any other exception parks `reconciliation-unavailable`.
   The lease deadline is not consulted, so observation works after expiry and
   while disabled. #26 routes a revoked attempt into this same path.
2. Receipt acceptance: shape, correlation, currency, and merge-permit checks still
   reject without recording; `unknown` still parks without recording (#31). A
   valid receipt is recorded (merge permit consumed if reported). Then, if
   `now > deadline` it fails `runtime-exhausted`, else if calls or cost exceed the
   lease it fails `budget-exhausted`: the failure is recorded and the run parks
   with that reason.
3. New work: `_begin_and_select` parks `disposition-required`, with no external
   call, while `awaiting_disposition`. Execution (`verify_authority`, `select`)
   keeps `_call_before_deadline`.
4. `repair-cycle --dispose-attempt ID` takes the lock, requires a current lease
   with that ID and a recorded failure (otherwise exit 2 naming the current
   attempt or the absence of a failure), records `disposed_attempt`, and makes no
   external call. The next timer run on a later day may begin new work.

### Failure model

1. Actors and deployments: a local operator or the systemd timer running the
   one-shot command against one repository state file; the adapter client is the
   external boundary.
2. Invariants and assets: no `verify_authority`/`select` call once a lease
   deadline has passed or while a failed attempt is undisposed; a recorded lease
   or failure is never dropped without an operator disposition; at most
   `observation_call_limit` reconcile calls per attempt, each bounded by
   `observation_minutes`; legacy state decodes.
3. Accepted failure classes: a crash after incrementing but before the call wastes
   one observation (bounded by the limit; the operator may raise it); a reconcile
   call off the main thread is not timer-bounded (existing behavior for every
   adapter call, tests only).
4. Covered elsewhere: revocation detection and authority on reads (#26); budget
   admission (#29); dispatch records (#30); `unknown` outcome mapping and systemd
   wiring (#31); process-tree cancellation (#27, merged).

## Success

- An expired lease is reconciled once per run within the allowance; a late
  terminal receipt is recorded with `attempt_failure = runtime-exhausted`, and no
  selection follows.
- An over-budget receipt is recorded with `budget-exhausted`.
- After the allowance is spent, runs park `observation-exhausted` with no call.
- A failed attempt blocks new work (`disposition-required`) until
  `--dispose-attempt` names it; afterwards a later-day run selects once.
- Legacy state without the new fields decodes and an expired legacy lease is
  observed.

## Validation

Focused tests in `desloppify/tests/commands/test_repair_cycle.py`; the old
`test_expired_lease_does_not_make_another_adapter_call` is replaced. Guardrails:
`make lint typecheck arch ci-contracts tests tests-full package-smoke`.
