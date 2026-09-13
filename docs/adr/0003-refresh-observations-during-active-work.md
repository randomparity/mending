# 0003 — Refresh observations during active work

## Status

Accepted (2026-09-13)

## Context

An active plan currently prevents a normal scan whenever execution work remains.
The block avoids queue churn, but it also prevents routine observation refreshes.
The scan merge already preserves unresolved work when detector coverage is incomplete,
and post-scan reconciliation has a boundary-aware path.

## Decision

Allow normal scans during an active plan. Determine that active-refresh state before a
scan can mutate the plan, then preserve the active plan's queue order, decisions, and
associations while it is active. Run plan reconciliation only after the queue was already
drained or an explicitly attested force-rescan requests a new boundary.

## Consequences

Operators can refresh findings without `--force-rescan`. A partial scan may update its
observations but cannot resolve findings from detectors that did not run. An active refresh
does not turn a newly absent queue item into a plan mutation; the existing state save remains
the durability boundary before boundary reconciliation.

## Considered & rejected

- **Keep the active-cycle preflight block.** verified: `scan_queue_preflight` raises for
  remaining queued work, so routine refresh cannot reach the merge path.
- **Use force-rescan for routine refresh.** verified: its preflight message calls the path
  queue-destructive and requires an explicit attestation.
- **Regenerate clusters on every refresh.** judgment: it would discard the active plan's
  operator-owned associations rather than preserving the active-work lifecycle.
