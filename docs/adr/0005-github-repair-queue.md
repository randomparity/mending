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
evidence digests and without ADR-0004's transient prior-digest fields. For each
eligible item, it derives a deterministic marker from those digests, queries
GitHub across open and closed issues for that marker, and adopts exactly one
match. It creates an issue only when no match exists, then re-searches and
writes the resulting number and URL under the existing state lock.

The public body uses only the deterministic marker, evidence digest, and
allowlisted, normalized owner, contracts, and verification fields. Raw evidence,
paths, identifiers, summary text, and values that fail public-safe validation
are omitted. An ambiguous lookup, malformed CLI result, command failure, or
uncertain create result records no new link and creates no retry candidate until
a later sync can establish one result.

## Consequences

GitHub becomes the actionable queue while local state retains the durable link
and raw observation. Tests can inject adapter responses without credentials or
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
