# Promote validated concerns to a GitHub repair queue

## Problem

Validated concern state is local-only. Adept consumes GitHub issues, and a
retry after an interrupted create must not create duplicate public work.

## Scope

`repair-queue sync` reads existing local state and promotes only open
`concerns` items with both ADR-0004 hashes, no transient prior-digest fields,
and a matching durable revalidation attestation. The attestation is an explicit
promotion gate, not a scan-repetition inference, and state merging drops it when
either hash changes.
It uses an injected `gh` adapter to search all issue states by a deterministic
digest marker, adopts exactly one match, and creates only after an empty search.
When local state already has a matching durable link, it reads that issue by
number first and records its current open/closed state instead of relying on a
human-preserved body marker.
`--apply` enables writes; dry run reports intended adoption or creation without
mutating local or GitHub state. Before an apply create, the command rechecks and
persists a pending marker in a completed state-lock transaction; a GitHub
number, URL, and marker are written only after an unambiguous result. A pending
marker is search/adopt-only until an explicit attested recovery clears it. The
public body contains only static labels plus the deterministic identity, evidence,
and concern markers; every source string stays local. After external I/O, a
second lock transaction must confirm that current hashes, repository, and pending
marker match the attempted promotion before it writes a link. Every operation
requires an explicit repository identity. Revalidation must resolve the exact
identity through `gh repo view`; later operations pass the verified value to
`gh` and validate it against durable records.

## Failure model

- Actors and deployments: a local authenticated operator runs the one-shot
  command against one repository; GitHub and `gh` are external boundaries.
- Invariants and assets: one coherent concern maps to at most one GitHub issue
  per shared state file and repository; public text contains no source review
  content; state links, pending create markers, and revalidation attestations
  are durable.
- Accepted failure classes: unavailable `gh`, malformed output, ambiguity, and
  uncertain creates are fail-closed and need a later sync; no scheduler retries.
- Covered elsewhere: concern discovery is #3, freshness is #4, claims and
  execution are Adept/#6, and pilot authority is #7. Cross-checkout remote
  serialization would require new authority and is unsupported here.

## Success

- A complete current concern has a deterministic marker and, only when each
  revalidation attestation matches, a public-safe issue payload with structural
  ownership, contracts, verification, and provenance labels.
- Across the named states `open` and `closed`, one marker match is adopted,
  zero permits one `--apply` create, and more than one produces no mutation.
- A matching local link survives a human body edit and reconciles the linked
  issue's open/closed state by number; an absent or malformed linked issue is
  fail-closed and never causes a replacement create.
- A changed concern cannot receive an old post-create result: a mismatched
  revalidation, repository, or pending marker leaves the remote result unlinked.
- A nonzero `gh` result, malformed JSON, or lost create response leaves a
  durable pending marker; later sync can adopt a recovered marker but cannot
  issue a duplicate create without explicit attested recovery.
- A dry run has no GitHub or state side effect, and incomplete or revalidation-
  marked concerns are reported as skipped.

## Validation

- Focused tests fake `gh` responses for dry-run, adopt, create, ambiguous,
  failed, malformed, closed, human-edited linked, interrupted-create,
  persisted-pending crash, stale-result, repository-mismatch, revalidation, and
  pending-recovery paths.
- Parser/command tests prove `--apply` is required for writes, one shared-state
  lock serializes apply runs, an explicit repository reaches every adapter call,
  and only complete, current, revalidated concerns reach the adapter.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
  `make tests` must pass.
