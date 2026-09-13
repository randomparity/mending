# Promote validated concerns to a GitHub repair queue

## Problem

Validated concern state is local-only. Adept consumes GitHub issues, and a
retry after an interrupted create must not create duplicate public work.

## Scope

`repair-queue sync` reads existing local state and promotes only open
`concerns` items with both ADR-0004 hashes and no transient prior-digest fields.
It uses an injected `gh` adapter to search all issue states by a deterministic
digest marker, adopts exactly one match, and creates only after an empty search.
`--apply` enables writes; dry run reports intended adoption or creation without
mutating local or GitHub state. The state lock persists a GitHub number, URL,
and marker only after an unambiguous result. Public bodies use normalized
allowlisted fields; free-form evidence, paths, raw identifiers, and unsafe
values are never emitted.

## Failure model

- Actors and deployments: a local authenticated operator runs the one-shot
  command against one repository; GitHub and `gh` are external boundaries.
- Invariants and assets: one coherent concern maps to at most one GitHub issue;
  public text contains no unapproved raw review content; state links are durable.
- Accepted failure classes: unavailable `gh`, malformed output, ambiguity, and
  uncertain creates are fail-closed and need a later sync; no scheduler retries.
- Covered elsewhere: concern discovery is #3, freshness is #4, claims and
  execution are Adept/#6, and pilot authority is #7.

## Success

- A complete current concern has a deterministic marker and a public-safe
  issue payload with owner, contracts, verification, and provenance.
- Across the named states `open` and `closed`, one marker match is adopted,
  zero permits one `--apply` create, and more than one produces no mutation.
- A nonzero `gh` result or malformed JSON leaves state unlinked; a later sync
  can adopt a recovered marker without issuing a duplicate create.
- A dry run has no GitHub or state side effect, and incomplete or revalidation-
  marked concerns are reported as skipped.

## Validation

- Focused tests fake `gh` responses for dry-run, adopt, create, ambiguous,
  failed, malformed, closed, and interrupted-create reconciliation paths.
- Parser/command tests prove `--apply` is required for writes and that only
  complete, current concerns reach the adapter.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
  `make tests` must pass.
