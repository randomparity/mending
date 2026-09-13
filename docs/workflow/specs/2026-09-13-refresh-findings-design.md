# Refresh findings during active work

## Problem

Normal scans stop while an active plan has queued work. Operators therefore use a
force-rescan escape hatch or defer observations, even though the scan merge can retain
unexamined findings and persistence is already atomic.

## Scope

Permit a normal scan while a plan has active execution work. Determine active-refresh
state from the loaded plan before scan mutation. The scan must merge fresh observations,
preserve unfinished queue entries, decisions, and plan associations, and reserve every
plan reconciliation mutation for a queue that was already drained or an explicit
force-rescan. The current owners remain: preflight decides entry, workflow persists state
before plan reconciliation, and plan reconciliation owns plan mutation.

## Failure model

- Actors and deployments: local CLI operators and CI profile scans; this change governs
  normal local scans with an active plan.
- Invariants and assets at stake: persisted findings and operator-owned plan associations;
  state commits before any plan mutation.
- Accepted failure classes: unreadable plan or state follows its existing degraded path;
  an interrupted save raises before reconciliation, leaving the prior durable files intact.
- Covered elsewhere: detector coverage and absent-finding resolution are owned by
  `merge_scan`; explicit reset semantics remain owned by `--force-rescan`.

## Success

A normal scan with non-empty active work reaches generation and merge without resetting
the plan baseline, regenerating clusters, or removing a queued item that newly becomes
absent. For the state and plan fixtures named below, unexamined findings remain open after
partial detector output, active plan data remains byte-equivalent, and save failure prevents
plan writes.

## Validation

- Focused preflight tests prove active queues no longer reject normal scans while invalid
  force-rescan attestations still reject.
- Focused workflow tests prove save precedes reconciliation, a failing save performs no
  reconciliation, and the prior state file remains readable.
- Focused plan tests prove active queues perform no plan mutation or save.
- State merge tests cover partial detector output and corrupt or unreadable state through
  existing load/degraded behavior.
