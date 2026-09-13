# 0006 — Use one systemd timer for bounded external repair cycles

## Status

Proposed

## Context

Issue #6 needs an external daily trigger without adding a daemon or scheduler
provider layer. It must survive restart, prevent overlapping work, and leave
Adept as the owner of claims, review, execution, and merge.

## Decision

Ship one systemd service/timer recipe and a one-shot local wrapper. `OnCalendar`
is the sole window authority and uses the host timezone. The wrapper persists
only scheduler state and budgets, takes an exclusive repository lock, and
creates a unique attempt lease before external I/O. The lease carries the local
day key, deadline, call/cost ceilings, and one merge permit. Adept receives the
lease and a repository-bound authority proof, then returns a correlated receipt
with active, terminal, or unknown state; claim/PR reference; merge consumption;
and measured calls/cost/currency. Unknown, missing, or over-budget receipts park.
The wrapper reconciles a recorded lease before selection and permits one active
repair and one consumed merge permit in the local-day window. Runtime defaults
to 90 minutes and model calls default to 100; operators may lower or raise both.
The model and a positive cumulative API-cost cap are required configuration.
An Adept-owned authority verifier returns an immutable policy identity, revision,
repository binding, and opaque proof identifier; each non-success outcome parks.
Missed windows do not catch up. The service uses a dedicated unprivileged account
with restricted state, repository, and environment-file access.

## Consequences

The supported deployment is a systemd host. Configuration is explicit and
local; live work remains disabled until an approved target authority is
installed and enabled. Adept must expose the bounded verifier/lease/receipt
protocol; its implementation remains Adept-owned. A different scheduler needs
a future decision.

## Considered & rejected

- **Support several schedulers.** judgment: provider abstraction exceeds the
  one-recipe scope and makes restart semantics less testable.
- **Run a resident daemon.** judgment: a timer-triggered one-shot process has
  simpler failure and disable behavior.
- **Infer model or cost limits.** judgment: a missing budget must fail closed.
