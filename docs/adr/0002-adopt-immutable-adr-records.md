# 0002 — Adopt immutable ADR records

## Status

Accepted (2026-09-12)

## Context

The repository needs an auditable way to record architectural decisions and a
pull-request check that rejects malformed, removed, moved, or gutted records.
Issue #2 depends on this convention before it adds its lifecycle decision.

## Decision

Adopt numbered ADRs in `docs/adr/` and install the records gate with the ADR
profile enabled. The `records` workflow is advisory until a repository
administrator requires its status check in branch protection.

## Consequences

Future architectural decisions use one immutable numbered Markdown record.
Pull requests run the checker against their base commit. Repository
administrators retain ownership of branch-protection configuration.

## Considered & rejected

- **Document decisions without automated validation.** judgment: review alone
  cannot reliably detect a removed or malformed record.
- **Enable ADR and debt enforcement now.** judgment: this change owns ADR
  adoption; debt-policy usage is deferred to its future owner.
- **Require the status check in this change.** verified: issue #8 assigns
  branch-protection configuration to a repository administrator.
