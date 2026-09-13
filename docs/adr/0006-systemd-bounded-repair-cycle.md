# 0006 — Use one systemd timer for bounded external repair cycles

## Status

Proposed

## Context

Issue #6 needs an external daily trigger without adding a daemon or scheduler
provider layer. It must survive restart, prevent overlapping work, and leave
Adept as the owner of claims, review, execution, and merge.

## Decision

Ship one systemd service/timer recipe and a one-shot local wrapper. The timer
uses the host timezone and an operator-selected calendar window. The wrapper
persists only cycle state and budgets, takes an exclusive repository lock,
reconciles known Adept work before selecting new work, and permits at most one
active repair and one merge in the local-day window. Runtime defaults to 90
minutes and model calls default to 100; operators may lower or raise both.
The model and a positive cumulative API-cost cap are required configuration.
Absent authority, a cap, or proof parks before model work; missed windows do
not catch up.

## Consequences

The supported deployment is a systemd host. Configuration is explicit and
local; live work remains disabled until an approved target authority is
installed and enabled. A different scheduler needs a future decision.

## Considered & rejected

- **Support several schedulers.** judgment: provider abstraction exceeds the
  one-recipe scope and makes restart semantics less testable.
- **Run a resident daemon.** judgment: a timer-triggered one-shot process has
  simpler failure and disable behavior.
- **Infer model or cost limits.** judgment: a missing budget must fail closed.
