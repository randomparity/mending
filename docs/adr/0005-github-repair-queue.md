# 0005 — Use the installed GitHub CLI for repair-queue promotion

## Status

Accepted (2026-09-13)

## Context

Validated architectural concerns now have stable semantic identity and evidence
digests, but local scanner state cannot be the actionable work queue consumed by
the existing Adept workflows. Promotion must survive a process interruption,
reconcile operator changes and closed work, and avoid public disclosure of raw
review text.

## Decision

Add a one-shot `repair-queue sync` command backed by a small injected `gh`
subprocess adapter. The command is a dry run unless `--apply` is supplied.
It accepts only current `concerns` work items with complete identity and
evidence digests and without ADR-0004's transient prior-digest fields. Their
absence on a later unchanged scan is the required revalidation; the plan's
superseded record is historical provenance, not an unbounded promotion block.
For each eligible item, it derives a deterministic marker from those digests,
queries GitHub across open and closed issues for that marker, and adopts exactly
one match. Before its first create it rechecks and persists a pending marker in
a completed state-lock transaction. It creates only after that transaction,
then re-searches and writes the resulting number and URL only after one result.
A matching durable local link is read back by its issue number before marker
search, so a human-edited, closed, or dismissed issue is reconciled rather than
recreated when its body marker is absent.

The public body uses only the deterministic marker, evidence digest, and
static field labels. Owner, contracts, verification, evidence, identifiers,
summary text, and every other source value remain local; their deterministic
identity/evidence hashes provide public provenance without publishing arbitrary
review text.

An ambiguous lookup, malformed CLI result, command failure, or uncertain create
result leaves the pending marker in state and creates no link. Later syncs for a
pending marker only search and adopt; they never create again. An operator may
clear a pending marker only through an explicit attested recovery command after
checking GitHub, then a later sync can make a new first attempt.

`repair-queue revalidate ID --apply --attest TEXT` records a matching marker and
operator attestation under the state lock. Sync accepts only that durable,
current revalidation record. State merging drops it when either concern hash
changes. After external I/O, sync rechecks the current hashes, repository, and
pending marker in a second state-lock transaction before recording a link; a
stale result remains unlinked.

Every command requires an explicit `OWNER/REPO` identity. Revalidation resolves
it with `gh repo view --json nameWithOwner` and requires the exact requested
identity before writing state. Later operations pass that verified value to every
`gh` invocation and persist it with pending and linked records; a mismatched
state record is fail-closed. This avoids ambient-repository writes when an
operator supplies another state path.

The at-most-one guarantee applies to writers sharing one state file. The state
lock serializes those writers. Independent state files/checkouts are unsupported
and are reported as a scope checkpoint rather than coordinated remotely.

## Consequences

GitHub becomes the actionable queue while local state retains the durable link,
last-read GitHub state, pending-attempt marker, revalidation attestation, and
raw observation. Tests can inject adapter responses without credentials or
network access. Operators need `gh` authentication only for `--apply`; claims,
scope authorization, scheduling, and execution remain Adept/#6 concerns.

## Considered & rejected

- **Add an HTTP client dependency.** verified: the repository already uses the
  installed `gh` CLI for campaign tracking, while `pyproject.toml` has no
  GitHub SDK dependency.
- **Key work only by scanner ID.** verified: ADR 0004 records that file-addressed
  IDs change on rename while semantic concern hashes remain comparable.
- **Publish raw review evidence.** judgment: a public queue must not treat
  untrusted local review text as public-safe provenance.
