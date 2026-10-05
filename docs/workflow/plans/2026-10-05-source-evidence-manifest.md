# Plan: bounded source evidence manifest (#24)

Spec: [2026-10-05-source-evidence-manifest-design.md](../specs/2026-10-05-source-evidence-manifest-design.md)

**Goal:** record which commit and which source files a concern was checked
against, and report when that evidence stops being current.

**Architecture:** one engine module, `desloppify/engine/repair_manifest.py`,
owning the manifest type, the git-revision builder, the stored-record parser,
the comparison, and approval rebinding. Nothing calls it yet; #25 wires it.

**Tech stack:** Python 3.11+, stdlib only, installed `git` CLI, pytest.

Expected implementation size: 380–450 changed lines (M) — ~190 lines for the
module and ~220 for one test file covering seven Success items.

## Global Constraints

- No new dependency; no change to `repair_queue.py`, the `repair-queue` CLI,
  state shape, or any file under `desloppify/app/commands/repair_cycle*` or
  `desloppify/engine/repair_cycle.py`.
- No ADR (ADR 0008 is #25's). Do not edit merged ADRs.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`,
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
  Focused loop: `uv run --locked pytest -q desloppify/tests/repair_queue/test_manifest.py`.

## File map

| File | Owns after change |
|---|---|
| `desloppify/engine/repair_manifest.py` (new) | manifest types, builder, record parser, comparison, approval rebinding |
| `desloppify/tests/repair_queue/test_manifest.py` (new) | fixture-repository tests for every Success item |

No ownership transition: `repair_queue.py` keeps record normalization; the
approval kind name is the only shared string.

## Task 1 — Manifest type, builder, stored record

**Interfaces (produced):**

```python
MANIFEST_SCHEMA = "desloppify-source-manifest:v1"
ROLES = frozenset({"implementation", "sibling", "test", "decision"})
MAX_DEPENDENCIES = 64
MAX_PATH_BYTES = 1024
@dataclass(frozen=True)
class DependencySpec: path: str; role: str
@dataclass(frozen=True)
class Dependency: path: str; role: str; status: str; object_id: str | None
@dataclass(frozen=True)
class SourceManifest:
    revision: str; dependencies: tuple[Dependency, ...]; coverage: str
    def as_record(self) -> dict[str, Any]
    @property
    def digest(self) -> str
@dataclass(frozen=True)
class AnalysisUnknown: reason: str
def build_manifest(root: Path, revision: str, dependencies: Sequence[DependencySpec], *,
                   coverage_declared_complete: bool) -> SourceManifest | AnalysisUnknown
def manifest_from_record(record: object) -> SourceManifest | AnalysisUnknown
```

**Verification:**

- Builder statuses and coverage — `focused-test`: `test_build_records_present_dependencies`,
  `test_unusable_dependencies_are_recorded_and_partial` (parametrized: missing,
  symlink, directory, `../x`, `/abs`, `a//b`, `a/`, `""`, oversize path),
  `test_undeclared_coverage_is_partial`; red: `ModuleNotFoundError`.
- Unknowns — `focused-test`: `test_unbindable_analysis_is_unknown` (bad
  revision, not a repository, unknown role, duplicate path, 65 dependencies);
  red: `ModuleNotFoundError`.
- Read isolation — `focused-test`: `test_pathspec_magic_is_literal`,
  `test_inherited_git_dir_is_ignored`; red: `ModuleNotFoundError`.
- Record round trip — `focused-test`: `test_record_round_trips`,
  `test_malformed_record_is_unknown` (non-mapping, wrong schema, bad status,
  present without object id, unsorted/duplicate paths, bad coverage); red:
  `ModuleNotFoundError`.

**Steps:**

1. Write the tests with a `repo` fixture: `git init -q` in `tmp_path`, files
   `src/impl.py`, `src/sibling.py`, `tests/test_impl.py`,
   `docs/adr/0001-rule.md`, `README.md`, committed with fixed author env; a
   `commit(repo, path, text) -> str` helper returns the new HEAD. Run the
   focused loop → red (`ModuleNotFoundError`).
2. Implement. `_git(root, *args)` runs `[git, "--literal-pathspecs", "-C",
   str(root), *args]` with `capture_output=True, text=True, timeout=30,
   check=False` and `env` = `os.environ` minus every `GIT_*` key; `git` from
   `shutil.which`; `FileNotFoundError`, `OSError`, `TimeoutExpired`, or a
   nonzero exit → `None`.
   `build_manifest`: reject >64 specs, a role outside `ROLES`, or a duplicate
   path with `AnalysisUnknown`; resolve `rev-parse --verify --end-of-options
   <revision>^{commit}` (failure → unknown); per spec, an invalid path →
   `unsupported`; else `ls-tree -z --full-tree <commit> -- <path>` (failure →
   unknown), split on NUL, keep the entry whose path equals the spec path:
   none → `missing`; mode `100644`/`100755` and type `blob` → `present` with
   its object id; otherwise `unsupported`. Sort by path. Coverage per spec.
   `as_record` returns `{"schema", "revision", "coverage", "dependencies":
   [{"path", "role", "status", "object_id"}]}`; `digest` is SHA-256 of
   `json.dumps(as_record(), sort_keys=True, separators=(",", ":"))`.
   `manifest_from_record` validates every field (schema equal, revision a
   40- or 64-char lowercase hex string, coverage in `complete|partial`,
   statuses in `present|missing|unsupported`, `object_id` hex iff present,
   roles in `ROLES`, paths strictly increasing, ≤64 entries, coverage equal
   to the builder rule applied to the stored statuses with
   declared-complete taken as `coverage == "complete"`) and returns
   `AnalysisUnknown("malformed manifest record")` otherwise.
3. Focused loop → green; `make lint typecheck`; commit
   `feat: add bounded source evidence manifest builder`.

## Task 2 — Comparison and bound approvals

**Interfaces (produced):**

```python
APPROVAL_KINDS = ("github_repair_revalidated",)
@dataclass(frozen=True)
class ManifestComparison:
    current: bool; reason: str; changed_paths: tuple[str, ...]; base_changed: bool
    previous_digest: str | None; current_digest: str | None
def compare_manifests(previous: SourceManifest | AnalysisUnknown,
                      current: SourceManifest | AnalysisUnknown) -> ManifestComparison
def retain_bound_approvals(detail: Mapping[str, Any],
                           comparison: ManifestComparison) -> dict[str, Any]
```

**Verification:**

- Change matrix — `focused-test`: `test_unchanged_evidence_is_current`,
  `test_single_dependency_edit_invalidates` (parametrized over
  implementation, sibling, test, decision paths; asserts `changed_paths ==
  (path,)`), `test_unrelated_edit_with_complete_coverage_is_current`
  (`base_changed` true); red: `ImportError: compare_manifests`.
- Partial and unknown — `focused-test`: `test_partial_coverage_never_current`,
  `test_unknown_never_current`; red: `ImportError`.
- Approvals — `focused-test`:
  `test_source_edit_clears_approval_with_unchanged_prose` (detail keeps the
  same `concern_evidence_digest`; approval removed, other keys kept),
  `test_current_comparison_rebinds_approval`,
  `test_unbound_approval_is_removed`; red: `ImportError`.

**Steps:**

1. Write the tests; run focused loop → red.
2. Implement per the spec's rule order. Digests are `None` for an unknown
   side. Changed paths: the sorted union of paths whose `(role, status,
   object_id)` differ or exist on one side only. `base_changed` compares
   revisions when both are manifests, else false. `retain_bound_approvals`
   copies `detail`; for each kind in `APPROVAL_KINDS` present, keep
   `{**record, "manifest_digest": current_digest}` only when
   `comparison.current`, the record is a mapping, and its
   `manifest_digest == previous_digest`; otherwise delete the key.
3. Focused loop → green; full guardrails; commit
   `feat: compare source manifests and clear stale approvals`.

## Deferrals

None.
