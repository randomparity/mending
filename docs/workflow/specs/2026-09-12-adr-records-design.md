# ADR records gate design

## Problem

The repository lacks an immutable ADR convention and automated validation, so
architectural decisions can be malformed or disappear without a dedicated PR check.

## Scope

Install the records gate, enable its ADR profile, and create ADR 0002. The
workflow remains advisory; branch protection is outside this change.

## Failure model

- Actors and deployments: contributors and GitHub pull-request runners.
- Invariants and assets at stake: ADR files remain numbered, structured, and append-only.
- Accepted failure classes: an advisory workflow can be bypassed until an administrator requires it.
- Covered elsewhere: branch protection is owned by the repository administrator.

## Success

The six gate assets are installed, only the three Bash entry points are executable,
the workflow selects `adr`, and the local suite and checker pass with ADR 0002.

## Validation

Run `./.github/scripts/check-records-test.sh` and
`RECORD_PROFILES=adr ./.github/scripts/check-records.sh`; both exit zero.
