# Run one bounded repair cycle from systemd

## Problem

An external daily trigger needs durable bounds and safe restart behavior while
delegating repair authority and execution to Adept.

## Scope

Add one systemd service/timer recipe, a configured one-shot wrapper, and local
cycle state. The timer interprets its configured calendar window in the host
timezone. The wrapper accepts an operator-selected window, runtime, model call
limit, positive cumulative API-cost cap, and orchestrator model; runtime and
calls default to 90 minutes and 100. It locks one repository, reconciles an
existing Adept claim or PR before selection, and starts no model work without
the required configuration and approved target authority. A missed window is
recorded as skipped and does not create catch-up work.

### Failure model

- Actors and deployments: a local systemd service invokes one configured
  repository; Adept, GitHub, and model APIs are external boundaries.
- Invariants and assets: one active repair and one merged repair per local-day
  window; scheduler state survives restart; authority cannot be inferred.
- Accepted failure classes: unavailable external services, expired runtime,
  exhausted budgets, and incomplete proof park the repository for that window.
- Covered elsewhere: concern discovery #3, revalidation #4, GitHub promotion
  #5, Adept claims/review/merge, and the live pilot #7.

## Success

- A configured timer invokes one one-shot cycle in its host-local window.
- Concurrent invocations for the same repository produce one active runner.
- Restart reconciles the recorded claim/PR before a new selection.
- A cycle cannot exceed its configured runtime, calls, cost cap, one active
  repair, or one merge; a missing cost cap, model, or authority parks safely.
- Disable prevents new work; re-enable resumes reconciliation rather than
  replaying a missed window.

## Validation

- Focused tests construct overlap, restart after claim/create/merge, window
  rollover, timeout, budget exhaustion, disabled, stale-base, and revoked-
  authority states; each proves no unpermitted new repair or merge occurs.
- A systemd recipe test validates the service/timer arguments, user-selected
  calendar value, host-local interpretation, and no catch-up setting.
- Integration fakes the Adept adapter; no test calls a live model, GitHub, or
  target repository.
