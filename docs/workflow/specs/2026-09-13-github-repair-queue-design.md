# Promote validated concerns to a GitHub repair queue

## Problem

Validated concern state is local-only. Adept consumes GitHub issues, and a
retry after an interrupted create must not create duplicate public work.

## Scope

`repair-queue sync` reads existing local state and promotes only open
`concerns` items with both ADR-0004 hashes and no transient prior-digest fields.
The next unchanged scan clears those fields and is the revalidation gate; the
associated plan supersession entry remains historical provenance, not a block.
It uses an injected `gh` adapter to search all issue states by a deterministic
digest marker, adopts exactly one match, and creates only after an empty search.
`--apply` enables writes; dry run reports intended adoption or creation without
mutating local or GitHub state. Before an apply create, the command rechecks and
persists a pending marker in a completed state-lock transaction; a GitHub
number, URL, and marker are written only after an unambiguous result. A pending
marker is search/adopt-only until an explicit
attested recovery clears it. Public bodies use only required lower-case phrases
that pass ADR-0005's one-to-eight-word grammar and structural/denylist checks;
free-form evidence, paths, raw identifiers, URLs, addresses, IPs, secret-like,
and instruction-like values are never emitted. A required unsafe value skips
promotion.

## Failure model

- Actors and deployments: a local authenticated operator runs the one-shot
  command against one repository; GitHub and `gh` are external boundaries.
- Invariants and assets: one coherent concern maps to at most one GitHub issue
  per shared state file; public text contains no unapproved raw review content;
  state links and pending create markers are durable.
- Accepted failure classes: unavailable `gh`, malformed output, ambiguity, and
  uncertain creates are fail-closed and need a later sync; no scheduler retries.
- Covered elsewhere: concern discovery is #3, freshness is #4, claims and
  execution are Adept/#6, and pilot authority is #7. Cross-checkout remote
  serialization would require new authority and is unsupported here.

## Success

- A complete current concern has a deterministic marker and, only when each
  required value passes the public grammar, a public-safe issue payload with
  owner, contracts, verification, and provenance.
- Across the named states `open` and `closed`, one marker match is adopted,
  zero permits one `--apply` create, and more than one produces no mutation.
- A nonzero `gh` result, malformed JSON, or lost create response leaves a
  durable pending marker; later sync can adopt a recovered marker but cannot
  issue a duplicate create without explicit attested recovery.
- A dry run has no GitHub or state side effect, and incomplete or revalidation-
  marked concerns are reported as skipped.

## Validation

- Focused tests fake `gh` responses for dry-run, adopt, create, ambiguous,
  failed, malformed, closed, interrupted-create, persisted-pending crash, and
  pending-recovery paths.
- Parser/command tests prove `--apply` is required for writes, one shared-state
  lock serializes apply runs, and only complete, current, public-safe concerns
  reach the adapter.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
  `make tests` must pass.
