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

The public body uses only the deterministic marker, evidence digest, and
allowlisted, normalized owner, contracts, and verification fields. Every source
field must be a lower-case, single-line phrase of one to eight words matching
`[a-z][a-z0-9_-]{0,31}`. It must not contain a path separator, dot, colon, at
sign, URL or address pattern, IP literal, opaque 32+-hex value, or a word from
the denylist `credential`, `host`, `ignore`, `instruction`, `key`, `password`,
`prompt`, `secret`, `system`, or `token`. A rejected required field prevents
promotion rather than being emitted or silently replaced. Raw evidence,
identifiers, summary text, and unapproved fields are never emitted.

An ambiguous lookup, malformed CLI result, command failure, or uncertain create
result leaves the pending marker in state and creates no link. Later syncs for a
pending marker only search and adopt; they never create again. An operator may
clear a pending marker only through an explicit attested recovery command after
checking GitHub, then a later sync can make a new first attempt.

The at-most-one guarantee applies to writers sharing one state file. The state
lock serializes those writers. Independent state files/checkouts are unsupported
and are reported as a scope checkpoint rather than coordinated remotely.

## Consequences

GitHub becomes the actionable queue while local state retains the durable link,
pending-attempt marker, and raw observation. Tests can inject adapter responses
without credentials or network access. Operators need `gh` authentication only
for `--apply`; claims, scope authorization, scheduling, and execution remain
Adept/#6 concerns.

## Considered & rejected

- **Add an HTTP client dependency.** verified: the repository already uses the
  installed `gh` CLI for campaign tracking, while `pyproject.toml` has no
  GitHub SDK dependency.
- **Key work only by scanner ID.** verified: ADR 0004 records that file-addressed
  IDs change on rename while semantic concern hashes remain comparable.
- **Publish raw review evidence.** judgment: a public queue must not treat
  untrusted local review text as public-safe provenance.
