# Bounded source evidence manifest with change invalidation

Issue: #24 (part of #17). Builds on the stable concern key of #23
([spec](2026-10-05-stable-concern-key-design.md)) and the ownership split of
[ADR 0007](../../adr/0007-host-driven-maintenance.md). Not wired into
`repair-queue sync`; #25 consumes this API and records ADR 0008.

## Problem

Concern freshness is proven only by normalized concern prose (ADR 0004). An
edit to the implementation, a compared sibling or caller, a test, or a
governing decision file leaves the evidence digest unchanged, and nothing
records which repository revision or source files a concern was checked
against, or whether that analysis was complete.

## Scope

New module `desloppify/engine/repair_manifest.py`, tests in
`desloppify/tests/repair_queue/test_manifest.py`. No other production file
changes.

- **Input.** The caller supplies the checkout root, a revision expression
  (for example the default branch), a sequence of `DependencySpec(path,
  role)` with role in `implementation`, `sibling` (compared siblings and
  callers), `test` (tests and contracts), `decision` (governing decision
  files), and `coverage_declared_complete: bool` — the caller's statement
  that the list names every relevant dependency. Deriving the list from a
  concern is #25's.
- **Builder.** `build_manifest` resolves the revision to a full commit ID and
  reads each dependency from that commit's tree with one non-recursive
  `git ls-tree` per path (a batched call descends into a directory named
  beside one of its files), never from the working tree. Each dependency is recorded as `present` (regular file,
  mode 100644/100755, with its git blob ID as content digest), `missing` (no
  entry at that exact path), or `unsupported` (symlink, gitlink, directory,
  or an invalid path). A valid path is a non-empty relative POSIX path of at
  most 1024 UTF-8 bytes with no NUL, no leading `/`, no trailing `/`, and no
  empty, `.` or `..` segment. Coverage is `complete` only when the caller
  declared it complete, at least one `implementation` dependency exists, and
  every dependency is `present`; otherwise `partial`.
- **Unknown.** `AnalysisUnknown(reason)` is returned, never raised, when the
  analysis cannot be bound: git unavailable, timed out (30 s) or failing; the
  revision unresolvable; an unknown role; a duplicate
  path; or more than 64 dependencies. An unknown is never current.
- **Digest.** `SourceManifest.digest` is SHA-256 of canonical JSON
  (`schema: desloppify-source-manifest:v1`, revision, coverage, dependencies
  sorted by path) — the same record `as_record()` returns for persistence.
  It proves which input was read, not that the concern is valid.
  `manifest_from_record` parses a stored record and returns
  `AnalysisUnknown` for anything malformed.
- **Comparison.** `compare_manifests(previous, current)` returns
  `ManifestComparison(current, reason, changed_paths, base_changed,
  previous_digest, current_digest)`, first match wins:
  1. either side unknown → not current, `unknown`;
  2. either side `partial` → not current, `coverage-incomplete` (broad
     invalidation);
  3. dependency paths, roles, statuses or blob IDs differ → not current,
     `dependencies-changed`, `changed_paths` sorted;
  4. otherwise current, `unchanged` — a base change that touched no
     dependency (`base_changed` true) keeps eligibility.

  Whenever both sides are manifests, `changed_paths` lists every path whose
  role, status or blob ID differs or that exists on one side only, so a
  dependency deleted after a complete manifest is reported by path.
- **Approvals.** `retain_bound_approvals(detail, comparison)` returns a copy
  of a work-item detail. A record of an approval kind
  (`APPROVAL_KINDS = ("github_repair_revalidated",)`) survives only when the
  comparison is current and the record's `manifest_digest` equals
  `previous_digest`; it is rebound to `current_digest`. Every other approval
  record — including one with no `manifest_digest` — is removed. Other detail
  keys are untouched. Today `normalize_record` and scan merge drop
  `manifest_digest` from that record, so a rebound approval does not survive
  a rescan: the binding fails closed until #25 replaces the revalidation
  record shape and preserves the field.

## Failure model

- Actors and deployments: one local operator or the #6 timer unit running
  Mending against a checkout it controls; the dependency list comes from
  imported model output (untrusted text) via #25.
- Invariants and assets: a source change to any recorded dependency makes
  the comparison not current even when concern prose is unchanged; partial
  or failed analysis is never current; reads stay inside the repository's
  object database at one resolved commit.
- Accepted failure classes:
  - A relevant file the caller omitted while declaring coverage complete is
    not tracked; completeness is the caller's claim (#25 owns derivation).
  - Uncommitted working-tree edits are invisible: evidence is bound to a
    commit, which is what a repair branches from.
  - A manifest with partial coverage can never become current; its cost is
    that such a concern is ineligible until coverage is complete.
- Covered elsewhere: deriving dependencies, storing manifests, preserving
  `manifest_digest` through record normalization and scan merge, and the
  pre-publication and pre-mutation recheck (#25); host adapter (#19).

### Threat model

- Boundary inventory: adds dependency paths and the revision expression
  (model- or operator-supplied) flowing into a fixed `git` argv; adds stored
  manifest records read back from the state file.
- Actor model: a model output or edited state file may carry hostile paths
  or revisions; the operator who runs Mending is trusted.
- Controls: argv list without a shell; `--literal-pathspecs` so `:(...)`
  magic is not interpreted; path validation above; `--end-of-options`
  before the revision; exact path equality on `ls-tree` output; `GIT_*` environment
  variables removed so `-C <root>` selects the repository; count, length and
  timeout bounds; malformed stored records parse to unknown.
- Out of scope: a hostile repository's git configuration (the operator
  chose the checkout); blob content is never read, so content size is not
  a resource concern.

## Success

1. Unchanged evidence: rebuilding at the same commit yields the same digest
   and a current comparison.
2. An edit to only the `implementation`, only a `sibling`, only a `test`, or
   only a `decision` dependency makes the comparison not current with exactly
   that path in `changed_paths`.
3. An unrelated edit with complete coverage is current with `base_changed`.
4. After a source edit with the concern's prose and evidence digest
   unchanged, the comparison is not current and the bound approval is
   removed from the detail.
5. Missing, symlinked, directory, and invalid-path dependencies are recorded
   with their status and make coverage `partial`; an undeclared-complete list
   is `partial`; each partial comparison is `coverage-incomplete`, and a
   dependency deleted after a complete manifest appears in `changed_paths`.
6. Bad revision, failing git, unknown role, duplicate path, too many
   dependencies, and malformed stored records yield `AnalysisUnknown`, and
   any comparison involving one is not current.
7. Pathspec magic and inherited `GIT_DIR` do not redirect what is read.

## Validation

Local fixture repositories created with `git init` under `tmp_path`, in
`desloppify/tests/repair_queue/test_manifest.py`; each Success item maps to a
focused test named in the plan. Guardrails: `make lint typecheck arch
ci-contracts tests tests-full package-smoke` and the records gate.
