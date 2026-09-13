# Adopt immutable ADR records

Install a repository-owned, profile-driven ADR validation gate and establish its
new accepted record. The implementation copies the six coordinated gate assets,
enables only ADR validation in a dedicated workflow, and validates the installation.

## Global Constraints

Use the supplied gate assets as one atomic install. Do not alter existing CI,
Makefile targets, lifecycle behavior, debt policy, or branch protection.

Expected implementation size: 6,600–7,000 changed lines (L) — measured from the six gate
assets and one record. Design denominator: 7,000 changed lines — direct operator approval on
2026-09-13, replacing the prior estimate; the current branch measures 6,605 additions.

## File map

- `.github/scripts/`: checker, its suite, migrator, and two sourced profiles.
- `.github/workflows/records.yml`: dedicated advisory PR workflow.
- `docs/adr/0002-adopt-immutable-adr-records.md`: accepted adoption ADR.

## Task 1 — Install and configure the gate

Interfaces: the workflow invokes the three executable scripts; the checker sources
the two profiles. Copy the six assets, retain 0644 on profiles, and set
`RECORD_PROFILES: adr` in the workflow.

Verification: Mode: focused-test. The suite fails if an asset is absent and passes
with the complete set. Expected command: `./.github/scripts/check-records-test.sh` exits zero.

## Task 2 — Add the adoption ADR and validate it

Interfaces: ADR 0002 is consumed by the ADR profile. Write the accepted adoption
decision and run `BASE_SHA=$(git merge-base HEAD origin/main) RECORD_PROFILES=adr \
./.github/scripts/check-records.sh`. The pre-existing legacy ADR 0001 remains
a warning outside this change.

Verification: Mode: focused-test. The checker validates ADR structure and the
enabled profile. Expected command exits zero.
