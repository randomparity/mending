# 0012 — Require a bounded evidence-anchor check for repair promotion

## Status

Accepted (2026-10-05)

## Context

ADR 0008 binds a revalidated concern to the blob IDs of its concern file and
related files and rechecks them before each GitHub create and link write. It
states that a manifest proves which input was read, never that the concern is
valid, and leaves `revalidate` as the operator's assertion that the concern
holds. #17 asks revalidation to run relevant bounded checks, keep failed,
partial, or unavailable results unknown, and invalidate on a changed check
outcome even when source is unchanged (#41). A `concerns` work item comes from
a model-driven review batch; its only machine-readable link to source is the
`PATH:LINE` citations and backtick-quoted code in its evidence text.

## Decision

- **Check.** The relevant check is an evidence-anchor predicate
  (`desloppify-repair-check:v1`): each `PATH:LINE[-LINE]` citation in the
  concern's evidence that resolves to a recorded present dependency must name
  lines that exist in that blob, and each identifier quoted in backticks in a
  citing item must occur in one of that item's cited files. Evidence with no
  resolved citation, an unreadable blob, or an exceeded item, byte, citation,
  or time bound is `unknown`.
- **Revalidation.** `revalidate` stores the check record and its digest beside
  the manifest; any other outcome refuses and removes a stored record. Only a
  record with a passing check whose digest matches is eligible. The digest
  covers the schema, outcome, and parsed claims, not reasons or bounds.
- **Recheck.** Where the manifest compares current under ADR 0008, the check
  is re-run against the rebuilt manifest. A different digest is not current
  and clears the revalidation, except that a transient `unknown` (unreadable
  blob, wall bound) skips the concern and keeps it.

ADR 0008 otherwise stays in force, including the lenient base move.

## Consequences

Every revalidation stored under ADR 0008 must be re-run. Concerns whose
evidence cites no recorded source line are never promoted. The review prompt
and skill docs do not ask reviewers for `PATH:LINE`, so promotion stays rare
until a separate change asks for it. Editing a concern's evidence after
revalidation invalidates it. The check proves that the reviewer's anchors
still hold, not that the concern is true, so `revalidate` remains partly the
operator's assertion. `check_concern` is the entry point the execution-time
recheck (#26/#31) calls.

## Considered & rejected

- **Re-run the originating detector.** judgment: cost; the detector is a
  model-driven review batch, neither bounded nor deterministic, and
  revalidation has no model runner.
- **Re-run the mechanical detectors over the recorded files.** verified:
  `rg -n "finding_ids|concern_fingerprint|source_issues"
  desloppify/intelligence/review/importing/holistic_issue_flow.py` at
  `ae9647ed` matches only the dismissed-concern fingerprint (line 106); a
  confirmed concern records no link to the mechanical findings that prompted
  it, so their outcome says nothing about this concern.
- **Execute the concern's `verification` text.** judgment: fit; it is free
  model-written text, and running it would execute untrusted instructions.
- **Treat a concern without citations as passing.** judgment: fit; #41
  requires an unsupported concern to be unknown, never valid.
- **Do nothing.** judgment: fit; a concern whose cited code is gone keeps
  promoting, which is the gap #41 records.
