# 0008 — Bind repair-queue promotion to current source

## Status

Accepted (2026-10-05)

## Context

ADR 0004 proves concern freshness only by digests of normalized concern prose,
and treats `protected_contracts` text as the sole governing-decision fixture.
ADR 0005 keys GitHub work by an identity+evidence marker, accepts a
`revalidate --attest TEXT` record as proof of freshness, requires attested
text to clear a pending attempt, and adopts any unique full-text search hit.
An edit to the code a concern describes changes none of those inputs. ADR
0007 assigns source-bound revalidation to Mending (#17).

## Decision

- **Stable key.** GitHub work and local link/pending records are keyed by
  `concern_key(repository, identity)`; the evidence digest is a record
  version, not part of the key. Legacy marker records are recognized only for
  migration (#23).
- **Evidence manifest.** A concern's source evidence is a manifest of git blob
  IDs read at one resolved commit of the operator's checkout: the concern file
  as `implementation`, its related files as `sibling`, declared complete as
  the reviewed input set (#24). A manifest proves which input was read, never
  that the concern is valid.
- **Source-bound revalidation.** `revalidate` stores the key, repository,
  evidence digest, complete manifest, and manifest digest. Operator text
  proves nothing: `--attest` and attestation records are removed, and a
  pre-existing attestation record is not eligible. `recover` clears a pending
  attempt on `--apply` alone.
- **Recheck before mutation.** `sync` rebuilds and compares the manifest
  before reading GitHub for a concern, and again inside the state-lock
  transaction that precedes each `gh issue create` and each local link write.
  Any mismatch, partial coverage, or unknown analysis clears the revalidation
  and skips the concern. With complete coverage, a base move that touches no
  recorded dependency keeps the concern current.
- **Verified adoption.** A unique search hit is linked only when its issue
  body carries the stable-key line or the current legacy marker line.

ADR 0004's identity and evidence digests, plan-reference transfer, and
dismissal rules stay in force. ADR 0005's `gh` adapter, dry-run default,
pending-before-create, re-search after create, explicit repository identity,
and single-state-file guarantee stay in force.

## Consequences

Every pre-existing revalidation must be re-run against a checkout. A concern
without a concern file, or whose related files are missing, symlinked, or
invalid, is never promoted. Unrelated commits do not churn eligibility; an
omitted dependency is not tracked. Linking reads issue bodies but stores none
of their text. A legacy issue whose body marker predates the current evidence
is no longer adopted automatically. Execution-time rechecks belong to the host
adapter (#19). ADRs 0004 and 0005 are not edited; readers find the replaced
parts here.

## Considered & rejected

- **Keep `--attest` beside the manifest.** judgment: fit; two proofs of
  freshness where one is unverifiable leaves the weaker path open.
- **Invalidate on any base move.** judgment: cost; with complete coverage it
  churns eligibility on every unrelated commit (operator decision on #25).
- **Re-check only once before publication.** judgment: fit; source can move
  during the GitHub reads that precede the create.
- **Derive test and decision dependencies from concern prose.** judgment:
  complexity; `protected_contracts` and `verification` are free text with no
  path contract.
- **Trust a unique search hit.** verified: GitHub Docs, "Searching issues
  and pull requests", section "Search by the title, body, or comments": a
  query without an `in:` qualifier searches titles, bodies, and comments, so
  a key pasted in a comment on another issue would be linked.
