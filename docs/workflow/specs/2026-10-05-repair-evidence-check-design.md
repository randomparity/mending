# Bounded evidence check during repair-queue revalidation

Issue: #41 (part of #17). Builds on the source-bound revalidation of #25
([spec](2026-10-05-source-bound-revalidation-design.md),
[ADR 0008](../../adr/0008-source-bound-repair-promotion.md)). Decision record:
[ADR 0012](../../adr/0012-bounded-evidence-check.md).

## Problem

`repair-queue revalidate` binds a concern to the blob IDs of its concern file
and related files, but nothing checks that the concern's claim still matches
that source. A concern whose cited code is gone, with its files unchanged since
revalidation or changed before it, is promoted as current. #17 asks for
relevant bounded checks whose failed, partial, or unavailable result stays
unknown.

## What "relevant check" means here

A `concerns` work item is produced by a model-driven review batch. Re-running
that detector is neither bounded nor deterministic and needs a model runner
that revalidation does not have, so it is not the check. The check is a
concern-specific predicate over what the review recorded: **evidence anchors**.
Each `detail.evidence` string may cite source as `PATH:LINE` or
`PATH:START-END` and quote code in backticks. The check passes only when every
resolvable citation names lines that exist in the recorded blob, and every
identifier quoted in backticks in a citing evidence item occurs as a whole
word in one of that item's cited files. It proves the reviewer's anchors still
hold, never that the concern is valid. The review prompt does not yet ask for
`PATH:LINE` citations (follow-up outside this change), so until it does most
newly imported concerns are `unknown` and are not promoted.

## Scope

**Runner** (new `desloppify/engine/repair_check.py`):
`check_concern(root, manifest, issue) -> CheckResult` with `outcome` in
`pass | fail | unknown`, a fixed-category `reason`, and the parsed `claims`.
`CheckResult.as_record()` is `{schema: "desloppify-repair-check:v1", outcome,
reason, transient, claims: [{citations: [{path, start, end}], identifiers:
[...]}], bounds}`. `digest` is the SHA-256 of the canonical JSON of `{schema,
outcome, claims}` only, so rewording a reason or retuning a bound does not
invalidate stored records; a change to what is checked bumps the schema.
`passing_check(record, digest)` is true only for a mapping with the v1 schema,
outcome `pass`, and a matching digest. `check_concern` is the reusable entry
point for the execution-time recheck (#26/#31).

Parsing, in evidence order: a citation is a `PATH:LINE[-LINE]` token whose
path has an extension and is not preceded by a word, `.`, `/`, or `-`
character. `PATH` resolves to the present dependency with that exact path,
else to the single present dependency ending in `/PATH`; a token resolving to
none or several (a version string, a host:port, an unrecorded caller) is
ignored. A backtick span is skipped when it contains a citation token or a
`/`, or resolves as a path; otherwise its identifiers
(`[A-Za-z_][A-Za-z0-9_]{2,}`) are checked. Evidence items without a resolved
citation are not checked. Lines are counted on the blob text as decoded by
`git` with universal newlines, split on `\n` only.

Outcomes:
- `unknown`, input-derived (`transient: false`): evidence is not a list of
  strings; more than 32 items or 16 KiB; no resolved citation (unsupported
  concern); more than 64 resolved citations (`MAX_DEPENDENCIES`); a cited blob
  over 1 MiB or more than 8 MiB in total.
- `unknown`, transient (`transient: true`): a cited blob unreadable or not
  UTF-8, or the 10 s wall bound reached. Every `git` call gets the remaining
  time as its timeout, so the check never runs past the bound.
- `fail`: a citation with `start < 1`, `end < start`, or `end` past the last
  line; or a quoted identifier absent from the item's cited files.
- `pass`: otherwise.

**Blob read** (`repair_manifest`): `blob_size(root, object_id, timeout)`
(`git cat-file -s`) and `read_blob(root, object_id, timeout)` (`git cat-file
blob`) through the existing `_git`, which gains an optional `timeout`
(default unchanged); both return `None` on any failure.

**Revalidation record** (`github_repair_revalidated`, persisted shape
extended): adds `check` (the record above) and `check_digest`.
`normalize_record` accepts it only when the manifest conditions of ADR 0008
hold and `passing_check(check, check_digest)`; it returns all seven fields, so
the scan merge keeps them. A record without a passing check (every #25
record) is not eligible until revalidated again.

**`revalidate`**: after a complete manifest, runs the check against it. Any
outcome but `pass` removes an existing `github_repair_revalidated` record,
then raises a `CommandError` naming the outcome and reason.

**Recheck** (`_comparison` / `_recheck_locked`): when the manifest compares
current, re-run the check against the rebuilt manifest. Transient `unknown` →
skip and keep the record (like an unreadable checkout). Otherwise a digest
different from `check_digest` → not current, clear the record, reason
`check-unknown`, `check-failed`, or `check-changed` (a different passing
record, for example edited evidence). Equal digest → current. Manifest outcomes are unchanged, including
the lenient base move.

**Docs**: README's repair-queue paragraph names the check, the `PATH:LINE`
citation it needs, and that concerns revalidated before it must be
revalidated again (they drop out of `sync` silently otherwise).

Tests inject results through `args.check(issue, manifest)`; the command uses
`check_concern(_source(args).root, ...)` otherwise.

## Failure model

- Actors and deployments: one local operator or the #6 timer unit running
  Mending against a checkout it controls; evidence text is imported model
  output.
- Invariants and assets: no create or link write proceeds unless the locked
  recheck found the manifest current and the check digest equal to the stored
  passing digest; revalidate stores a record only for `pass`; links and
  pending records are never discarded by a check.
- Accepted failure classes:
  - Anchors hold while the concern is no longer true; the check proves anchors
    only (ADR 0012).
  - A concern whose evidence cites no `PATH:LINE` is never promoted (issue
    criterion: unsupported is unknown).
  - A quoted identifier can match unrelated code in the same file.
  - A backtick span naming something absent from the cited files (prose in
    backticks, a name from another file) fails; the reviewer's anchor no
    longer holds as written.
  - A transient read failure keeps a record that is skipped until the read
    succeeds; checks run under the state lock, as ADR 0008's manifest reads
    do, for at most the 10 s bound.
  - Until the review prompt asks for `PATH:LINE` (follow-up candidate), few
    new concerns are promoted.
- Covered elsewhere: execution-time recheck (#26/#31); classification (#21);
  selection (#22); briefs (#18/#20); live publication (#7).

### Threat model

- Boundary widened: evidence text (model output) now selects which recorded
  blobs are read and is parsed by two fixed regular expressions.
- Actors: imported model output; the operator is trusted.
- Controls: citations resolve only to manifest-recorded present blobs by
  object ID (no evidence text reaches `git`); item, byte, citation, total-byte,
  and time bounds; reasons are fixed categories that never echo evidence.
- Out of scope: hostile repository git config (ADR 0008).

## Success

1. A fixture concern whose evidence cites an existing line passes; `revalidate`
   stores `check` and `check_digest`.
2. A cited line past the end of file, or a quoted identifier absent from the
   cited file, fails; `revalidate` refuses and removes any stored record. A
   backtick-wrapped citation or path is not a quote.
3. An over-bound blob, too many evidence items, too many citations, and the
   wall bound are `unknown`.
4. Evidence with no resolved citation is `unknown`; an unresolvable token
   beside a resolved citation is ignored.
5. Unchanged source with a changed check outcome (edited evidence, or an
   injected different, failing, or input-derived unknown result) clears the
   record on `sync --apply`; an injected transient `unknown` skips and keeps
   it.
6. Records lacking `check`, with a failing check, or with a mismatched
   `check_digest` are not eligible; a rescan keeps a valid record.

## Validation

Unit tests in `desloppify/tests/repair_queue/test_check.py` (runner over a
`git init` fixture), `test_promotion.py` (record shape), and
`desloppify/tests/commands/test_repair_queue.py` (injected results);
`test_sync_matrix.py` runs the real check end to end. Guardrails: `make lint
typecheck arch ci-contracts tests tests-full package-smoke` and the records
gate.
