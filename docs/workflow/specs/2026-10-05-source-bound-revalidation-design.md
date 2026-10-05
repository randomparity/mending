# Source-bound revalidation and pre-mutation recheck

Issue: #25 (part of #17). Consumes the stable key of #23
([spec](2026-10-05-stable-concern-key-design.md)) and the evidence manifest of
#24 ([spec](2026-10-05-source-evidence-manifest-design.md)). Decision record:
[ADR 0008](../../adr/0008-source-bound-repair-promotion.md), successor to the
changed parts of ADRs 0004 and 0005, building on ADR 0007.

## Problem

`repair-queue revalidate` stores operator text (`--attest`) as the
revalidation record and `sync` accepts any non-empty text as proof of
freshness. Nothing rereads source before publication or before the GitHub
create, so a concern whose code changed is still promoted. Search adoption
links a unique full-text hit even when the match is only a pasted key in a
comment. The #24 record normalizer and scan merge drop `manifest_digest`.

## Scope

**Dependencies of a concern** (`concern_dependencies(issue)`, engine): the
work item's `file` as `implementation` when it is a non-empty string other
than `.`, and each `detail.related_files` entry not equal to it as `sibling`,
deduplicated in order. The list is declared complete: it is the input set the
concern review named. A concern with no concern file has no implementation
dependency, so its manifest is `partial` and it is never eligible. A
`related_files` value that is not a list, or a non-string entry, is passed
through as an invalid spec so `build_manifest` returns `AnalysisUnknown`; an
invalid path string becomes an `unsupported` dependency (both ineligible).

**Source checkout** (`SourceCheckout(root, revision)`, engine):
`manifest_for(issue)` returns `build_manifest(root, revision,
concern_dependencies(issue), coverage_declared_complete=True)`. The command
builds it from `--source-root` (default: project root) and `--revision`
(default `HEAD`); tests inject `args.source`. Work-item paths are relative
to the project root, so the project root must be the repository top level;
otherwise `build_manifest` returns `AnalysisUnknown` and the `revalidate`
error names `--source-root`.

**Revalidation record** (`github_repair_revalidated`, persisted shape
replaced): `{key, repository, evidence_digest, manifest, manifest_digest}`,
where `manifest` is `SourceManifest.as_record()`. `normalize_record` accepts it
only when `manifest` parses, its coverage is `complete`, and `manifest_digest`
equals its digest; it returns all five fields. Scan merge already keeps the
normalized record while both concern hashes are unchanged, so the manifest
binding survives a rescan. Any record carrying `attestation` and no valid
manifest (every pre-#25 record) raises `RepairRecordError`: the concern is
not eligible until revalidated again. `retain_bound_approvals` and
`APPROVAL_KINDS` (no callers) are removed; the recheck below replaces them.

**`revalidate ID --repo R --apply`**: under the state lock, builds the
manifest; anything but a complete `SourceManifest` is a `CommandError`
naming the reason and writes nothing; otherwise stores the record. `--attest`
is removed from every action; `recover MARKER --repo R --apply` needs no text.

**Recheck** (`_recheck_locked`, inside an open state-lock transaction):
rebuild the manifest and `compare_manifests(stored, rebuilt)`. Current →
proceed. The checkout cannot be read (rebuilt side `AnalysisUnknown`: bad
`--revision`, wrong root, git failure) → skip and keep the record, so an
operator error never clears the queue. Otherwise not current → remove
`github_repair_revalidated`, print `Skipped <id>: source evidence is not
current (<reason>)`, and stop this candidate. A refused `revalidate` names
each dependency that is not `present` (or the missing concern file). A base move with complete
coverage and no recorded dependency change is current (operator decision,
lenient rule of #24); the stored record is not rewritten.

**Sync** checks at three points, each skipping on mismatch:
1. before any GitHub read for the candidate: compare without the lock; only
   on a mismatch (and only with `--apply`) open the lock, confirm the
   candidate is unchanged, and run `_recheck_locked` to clear (dry run prints
   without clearing);
2. in `_create_once`'s locked transaction, after the existing stale/peer
   checks and before the pending record is written — the last point before
   `gh issue create`;
3. in `_write_link`'s locked transaction before the link is written.
Lock, pending-before-create, correlation by key, and uncertain-create
behavior are otherwise unchanged.

**Search adoption**: `GitHubIssueClient.search` also requests `body`;
`GitHubIssue` gains `body: str = ""` (a non-string body is malformed JSON).
A hit is verified when `carries_concern_marker(body, candidate)` finds a
whole line equal to `<!-- desloppify-concern-key: KEY -->` or the current
legacy line `<!-- desloppify-concern: LEGACY -->`. `_search` keeps every hit;
the adoption decision counts only verified hits: one → link; more than one →
ambiguous skip; none while unverified hits exist → print `Skipped <id>:
GitHub matches do not carry the concern key` and neither link nor create;
no hits at all → the existing create / pending rules.

**Docs**: README's repair-queue lines drop `--attest` and the "neither
rereads current source" caveat.

## Failure model

- Actors and deployments: one local operator, or the #6 timer unit, running
  Mending against a checkout it controls; concern files come from imported
  model output; GitHub issue bodies and comments are writable by third
  parties.
- Invariants and assets: no create or link write proceeds unless the
  manifest compared current in the locked recheck immediately before it; at most one GitHub issue per key
  among writers sharing one state file; persisted links and pending records
  are never discarded by a recheck.
- Accepted failure classes:
  - A file the concern review omitted is not tracked (completeness is the
    review's claim; #24 accepted the same class).
  - Source changes after the last recheck and before `gh issue create`
    returns are not seen; the window is one subprocess call.
  - A third party who edits an issue body to carry the key can still make an
    item park as ambiguous or, when it is the only match, be adopted; the
    genuine issue normally exists and makes the match ambiguous.
  - A legacy issue whose body carries an older-evidence marker is no longer
    adopted automatically; the operator adds the key line to its body.
  - Operators must re-run `revalidate` once for every pre-#25 record.
  - `revalidate` is the operator's assertion that the concern holds at the
    bound revision; source that changed between the concern review and
    `revalidate` is not detected (concern import records no manifest).
  - A project root below the repository top level is unsupported: every
    revalidation is refused.
- Covered elsewhere: execution-time recheck — no execution path consumes
  repair-queue eligibility yet; owner tracked on #17; briefs (#18); contract docs (#16); live publication (#7);
  independent state files (excluded).

### Threat model

- Boundaries added: issue bodies from `gh issue list` (third-party text);
  concern file paths reaching `git` argv (from #24, now wired).
- Actors: third-party GitHub users; imported model output; the operator is
  trusted, including `--source-root` and `--revision`.
- Controls: bodies are compared by exact whole-line equality, never parsed,
  rendered, or stored; paths go through #24's validation, literal pathspecs,
  `--end-of-options`, and bounds; `gh` and `git` keep fixed argv.
- Out of scope: hostile repository git config (operator's checkout);
  authorship checks on issue bodies.

## Success

1. `revalidate` on a fixture repository stores the five-field record; a
   concern without a concern file, or with a missing related file, is
   refused and stores nothing; `revalidate` and `recover` reject `--attest`.
2. Records with `attestation` only, an unparsable manifest, partial
   coverage, or a mismatched `manifest_digest` are not eligible.
3. A rescan with unchanged concern hashes keeps `manifest` and
   `manifest_digest` and the item stays eligible.
4. Fixture matrix through `cmd_repair_queue("sync")` with a real
   `SourceCheckout` and an injected client: (a) unrelated commit after
   revalidation → created once; (b) dependency edit after revalidation →
   skipped, revalidation cleared, zero search and create calls; (c)
   dependency edit between the first check and the locked create recheck →
   zero creates and no pending record; (d) an existing closed link plus a
   dependency edit → link kept unchanged, revalidation cleared; (e) repeated
   syncs after a create → one create in total; (f) dependency edit inside
   `create` → one create, no link, pending kept, revalidation cleared; after
   `revalidate` and another sync the item links to that issue with no
   second create.
5. A search hit whose body lacks both marker lines is neither linked nor
   followed by a create; one verified hit is linked even beside unverified
   hits; two verified hits park.

## Validation

Unit tests in `desloppify/tests/commands/test_repair_queue.py` (fake source),
`desloppify/tests/repair_queue/test_promotion.py` (record shape, body marker,
client body decoding), `desloppify/tests/repair_queue/test_manifest.py`
(dependency derivation), and the fixture matrix in
`desloppify/tests/repair_queue/test_sync_matrix.py` using `git init` under
`tmp_path`. Guardrails: `make lint typecheck arch ci-contracts tests
tests-full package-smoke` and the records gate.
