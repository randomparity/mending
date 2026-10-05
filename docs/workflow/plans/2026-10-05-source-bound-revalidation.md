# Plan: source-bound revalidation and pre-mutation recheck (#25)

Goal: replace attested revalidation with a manifest-bound record and recheck
source before every repair-queue mutation, per
[the spec](../specs/2026-10-05-source-bound-revalidation-design.md) and
[ADR 0008](../../adr/0008-source-bound-repair-promotion.md).

Architecture: the engine owns dependency derivation, the record shape, and the
body-marker check (`repair_manifest.py`, `repair_queue.py`); the command module
only wires the lock, the source checkout, and GitHub I/O.

Tech stack: Python 3.11+, pytest, `git` and `gh` CLIs via fixed argv.

Expected implementation size: 380–480 changed lines (M) — about 130 production
lines across four modules plus README, and about 300 test lines (fixture
matrix, migrated unit fixtures, removed `retain_bound_approvals` tests).

## Global Constraints

- No new dependencies. Engine modules must not import `desloppify.app`.
- Fail closed: any unparsable record, unknown analysis, or partial coverage is
  ineligible. Never store issue-body text.
- Do not touch `repair_cycle*.py` or their tests (sibling campaign work).
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## File map

| File | Change |
|---|---|
| `desloppify/engine/repair_manifest.py` | add `concern_dependencies`, `SourceCheckout`; remove `APPROVAL_KINDS`, `retain_bound_approvals` |
| `desloppify/engine/repair_queue.py` | revalidation record shape; `GitHubIssue.body`; `search` requests body; `carries_concern_marker` |
| `desloppify/app/commands/repair_queue.py` | manifest-built `revalidate`; `_recheck_locked` at three points; verified adoption; drop `_attestation` |
| `desloppify/app/cli_support/parser_groups_repair_queue.py` | drop `--attest`; add `--source-root`, `--revision` |
| `README.md` | repair-queue command lines |
| tests | `tests/repair_queue/test_manifest.py`, `test_promotion.py`, new `test_sync_matrix.py`; `tests/commands/test_repair_queue.py` |

`merge_issues._preserve_repair_metadata` needs no change: it stores
`normalize_record`'s output, which now includes `manifest` and
`manifest_digest`.

## Task 1 — engine record shape, derivation, and body marker

Files: `desloppify/engine/repair_manifest.py`, `desloppify/engine/repair_queue.py`,
`desloppify/tests/repair_queue/test_manifest.py`, `desloppify/tests/repair_queue/test_promotion.py`.

Interfaces (produced):
`concern_dependencies(issue: Mapping[str, Any]) -> tuple[DependencySpec, ...]`;
`SourceCheckout(root: Path, revision: str).manifest_for(issue) -> SourceManifest | AnalysisUnknown`;
`GitHubIssue(number, url, state, body: str = "")`;
`carries_concern_marker(body: str, candidate: PromotionCandidate) -> bool`;
`KEY_LINE = "<!-- desloppify-concern-key: {} -->"`.

Verification:
- Contract: dependency derivation. Mode: focused-test —
  `test_concern_dependencies_*` in `test_manifest.py`; red: ImportError;
  green: `uv run --locked pytest -q desloppify/tests/repair_queue/test_manifest.py`.
- Contract: revalidation record shape. Mode: focused-test —
  `test_revalidation_requires_complete_bound_manifest` (parametrized over
  attestation-only, bad manifest, partial coverage, digest mismatch) and
  `test_scan_merge_keeps_manifest_binding` in `test_promotion.py`; red: the
  attestation record is accepted / manifest fields dropped.
- Contract: body marker and decoding. Mode: focused-test —
  `test_carries_concern_marker_*` and `test_search_requests_and_decodes_body`
  in `test_promotion.py`; red: ImportError / missing `body` in argv.

Code (repair_manifest.py):

```python
def concern_dependencies(issue: Mapping[str, Any]) -> tuple[DependencySpec, ...]:
    """The concern file as implementation and its related files as siblings."""
    detail = issue.get("detail")
    related = detail.get("related_files", []) if isinstance(detail, Mapping) else []
    concern_file = issue.get("file")
    specs = []
    if isinstance(concern_file, str) and concern_file not in {"", "."}:
        specs.append(DependencySpec(concern_file, "implementation"))
    for path in related if isinstance(related, list) else [related]:
        if path not in {spec.path for spec in specs}:
            specs.append(DependencySpec(path, "sibling"))
    return tuple(specs)


@dataclass(frozen=True)
class SourceCheckout:
    """The operator's checkout and the revision a concern is checked against."""

    root: Path
    revision: str

    def manifest_for(self, issue: Mapping[str, Any]) -> SourceManifest | AnalysisUnknown:
        return build_manifest(
            self.root, self.revision, concern_dependencies(issue),
            coverage_declared_complete=True,
        )
```

A non-string `related_files` entry flows into `DependencySpec` and
`build_manifest` returns `AnalysisUnknown("invalid dependency spec")`.

Code (repair_queue.py `_kind_fields` branch):

```python
if kind == "github_repair_revalidated":
    manifest = manifest_from_record(record.get("manifest"))
    if (
        not isinstance(manifest, SourceManifest)
        or manifest.coverage != "complete"
        or record.get("manifest_digest") != manifest.digest
    ):
        raise RepairRecordError("github_repair_revalidated has no complete bound manifest")
    return {"manifest": manifest.as_record(), "manifest_digest": manifest.digest}
```

`carries_concern_marker` returns true when any `line.strip()` of `body` equals
`KEY_LINE.format(candidate.key)` or
`f"<!-- desloppify-concern: {legacy_marker(candidate.identity, candidate.evidence_digest)} -->"`.
`render_issue` uses `KEY_LINE`. `search` adds `body` to `--json`;
`_decode_issues` requires `body` to be absent or a string. Delete
`APPROVAL_KINDS`, `retain_bound_approvals`, and their three tests; fixtures
`_REVALIDATED`/`_issue` gain `manifest`/`manifest_digest` from a literal
complete manifest record (one `implementation` dependency with a 40-hex blob).

## Task 2 — command wiring and CLI

Files: `desloppify/app/commands/repair_queue.py`,
`desloppify/app/cli_support/parser_groups_repair_queue.py`,
`desloppify/tests/commands/test_repair_queue.py`, `README.md`.

Interfaces (consumed): Task 1 names. (produced) `_recheck_locked(args, state,
candidate) -> bool`; `args.source` injection point.

Verification:
- Contract: `revalidate` stores the bound record and refuses partial/unknown.
  Mode: focused-test — `test_revalidate_stores_manifest_bound_record`,
  `test_revalidate_refuses_unbindable_source` (fake source returning
  `AnalysisUnknown` and a partial manifest); red: TypeError on missing attest.
- Contract: `--attest` removed. Mode: focused-test —
  `test_parser_rejects_attest` asserts `SystemExit` for
  `repair-queue revalidate ID --repo R --attest x`.
- Contract: recheck skips and clears at each point. Mode: focused-test —
  `test_sync_skips_and_clears_when_source_changed`,
  `test_create_recheck_mismatch_writes_no_pending`,
  `test_link_recheck_mismatch_keeps_link`; fake source switches manifests.
- Contract: verified adoption. Mode: focused-test —
  `test_unique_hit_without_marker_is_not_linked`; red: link written.
- Contract: README. Mode: task-test-not-applicable — prose with no executable
  consumer.

Code (command):

```python
def _source(args):
    supplied = getattr(args, "source", None)
    if supplied is not None:
        return supplied
    root = getattr(args, "source_root", None)
    return SourceCheckout(Path(root) if root else get_project_root(), args.revision or "HEAD")


def _comparison(args, issue) -> ManifestComparison:
    record = issue["detail"].get("github_repair_revalidated") or {}
    stored = manifest_from_record(record.get("manifest"))
    return compare_manifests(stored, _source(args).manifest_for(issue))


def _recheck_locked(args, state, candidate) -> bool:
    """Inside a state-lock transaction: proceed only on current source evidence."""
    comparison = _comparison(args, _issues(state)[candidate.issue_id])
    if comparison.current:
        return True
    _issues(state)[candidate.issue_id]["detail"].pop("github_repair_revalidated", None)
    print(f"Skipped {candidate.issue_id}: source evidence is not current ({comparison.reason}).")
    return False
```

`_sync_one` starts with `if not _source_current(args, state, candidate): return`;
`_source_current` in dry run prints the same message without clearing when
`_comparison` is not current; with `--apply` it opens `_locked_state`, returns
False on a stale `_candidate_by_id`, else `_recheck_locked`. `_create_once`
adds `or not _recheck_locked(...)` after the existing checks (its own skip
message only when the recheck passed); `_write_link` returns after the fresh
check when `_recheck_locked` is False. `_adopt_if_unique` checks
`carries_concern_marker(matches[0].body, candidate)` before linking.
`_revalidate` builds `_source(args).manifest_for(issue)` under the lock and
raises `CommandError(f"source evidence cannot be bound: {reason}")` for an
`AnalysisUnknown` or `coverage-incomplete` partial manifest. Remove `_attestation`.
Parser: remove `--attest`; add `--source-root` and `--revision` to `sync` and
`revalidate`. Unit tests use `_Source` (fake `manifest_for`) via
`args.source`, and `GitHubIssue` fixtures carry the key line in `body`.

## Task 3 — fixture sync matrix

File: `desloppify/tests/repair_queue/test_sync_matrix.py`.

Verification: Mode: focused-test — one test per spec Success 4(a)–(e) and 3,
each running `cmd_repair_queue` with `SourceCheckout(tmp repo, "HEAD")`, an
in-memory `state_data`, and a recording client whose `create` makes later
searches return a body carrying the key line; (c) commits a dependency edit
inside the client's `search`. Red: before Task 2, (b)–(d) create or link.
Green: `uv run --locked pytest -q desloppify/tests/repair_queue/test_sync_matrix.py`.
The fixture repo uses the `GIT_ENV` isolation of `test_manifest.py`.

## Rollback

Each task is one commit; `git revert` restores the attested path. Existing
attestation records stay in state files and become eligible again on revert.
