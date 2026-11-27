# Repair pipeline mypy coverage

## Problem

Issue #47 identifies repair modules omitted from the explicit mypy gate and
unchecked typing errors. Its operator scope addition also identifies stale
selection-guard reasoning after #60.

## Scope

Add the seven engine repair modules, three repair commands, and two repair
parser modules to the existing gate. Preserve its strictness. Express existing
runtime validation as type narrowing and construct briefs with typed inputs.
Keep current ownership, callers, validation, digest fields and selection behavior.
Correct only the obsolete damaged-state rationale in the selection design.
No new ADR or migration is required. The approved #47 exclusions remain binding:
plan repair-state commands, repository-wide typing, repair behavior changes,
ADR 0014 edits, and operational pilot/publication/timer runs are excluded.

### Failure model

Malformed or absent work items and malformed digest/function fields must retain
existing rejection behavior. Constructor typing must preserve subclass selection,
field values, wording and reviewed digests. Missing gate entries would leave
repair code unchecked. Deployment is the repository's default typecheck and
existing repair CLI tests; no live publication is part of this change.

## Success

The default mypy gate checks all 28 selected files without diagnostics, ignores
or blanket Any escapes. Existing valid and malformed repair tests pass. The
no-work selection guard stays; its documentation explains avoiding an unnecessary
lock and preserving the no-op while state_lock refuses fallback writes.

## Validation

- Mode: focused-test. Contract: default repair typing coverage and accurate
  narrowing. Red: explicit mypy over the twelve modules reports the unchecked
  errors. Green: `make typecheck`; compare the exact twelve additions and retain the
  original sixteen configured inputs, then verify the expanded 28-file gate.
- Mode: focused-test. Contract: unchanged valid/malformed repair inputs and
  brief digest behavior. Run existing repair queue, brief and selection tests
  before and after implementation, then repository guardrails.
- Mode: task-test-not-applicable. Surface: selection rationale prose. No
  executable consumer validates this explanation; compare with state_lock and
  retained guard directly without snapshotting wording.
