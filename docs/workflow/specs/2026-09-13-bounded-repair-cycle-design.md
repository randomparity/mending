# Run one bounded repair cycle from systemd

## Problem

An external daily trigger needs durable bounds and safe restart behavior while
delegating repair authority and execution to Adept.

## Scope

Add one systemd service/timer recipe, a configured one-shot wrapper, and local
cycle state. The timer's operator-selected `OnCalendar` is the sole window
authority and uses the host timezone. The wrapper accepts runtime, model-call
limit, positive cumulative API-cost cap, and orchestrator model; runtime and
calls default to 90 minutes and 100. It locks one repository, persists a unique
attempt lease before external I/O, verifies a repository-bound Adept authority
proof, and reconciles a recorded lease before selection. Adept receives the
lease/proof and returns a correlated receipt with state, claim/PR reference,
merge-permit consumption, and measured calls/cost/currency. Unknown, missing,
or over-budget receipts park; a missed timer window creates no catch-up work.

### Failure model

- Actors and deployments: a local systemd service invokes one configured
  repository; Adept, GitHub, and model APIs are external boundaries.
- Invariants and assets: one active repair and one consumed merge permit per
  local day; scheduler state and correlation survive restart; authority cannot
  be inferred.
- Accepted failure classes: unavailable external services, expired runtime,
  exhausted budgets, and incomplete proof park the repository for that window.
- Covered elsewhere: concern discovery #3, revalidation #4, GitHub promotion
  #5, Adept claims/review/merge, and the live pilot #7.

## Success

- A configured timer invokes one one-shot cycle in its host-local window.
- Concurrent invocations for the same repository produce one active runner.
- Restart reconciles the recorded claim/PR before a new selection.
- A cycle cannot exceed the deadline/limits enforced by its Adept lease, one
  active repair, or one merge permit; a missing cost cap, model, authority,
  correlation, or receipt parks safely.
- Disable prevents new work; re-enable resumes reconciliation rather than
  replaying a missed window.

## Validation

- Focused tests construct overlap, restart after claim/create/merge, window
  rollover, timeout, budget exhaustion, disabled, stale-base, and revoked-
  authority states; each proves no unpermitted new repair or merge occurs.
- A systemd recipe test validates the dedicated account, restricted paths,
  user-selected `OnCalendar`, host-local interpretation, and no-catch-up setting.
- Integration fakes the Adept adapter; no test calls a live model, GitHub, or
  target repository.
