from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from desloppify.engine.repair_manifest import (
    MAX_DEPENDENCIES,
    MAX_PATH_BYTES,
    AnalysisUnknown,
    DependencySpec,
    SourceManifest,
    build_manifest,
    compare_manifests,
    manifest_from_record,
    retain_bound_approvals,
)

FILES = {
    "src/impl.py": "def impl():\n    return 1\n",
    "src/sibling.py": "def sibling():\n    return 2\n",
    "tests/test_impl.py": "def test_impl():\n    assert True\n",
    "docs/adr/0001-rule.md": "# 0001 rule\n",
    "README.md": "readme\n",
}
SPECS = (
    DependencySpec("src/impl.py", "implementation"),
    DependencySpec("src/sibling.py", "sibling"),
    DependencySpec("tests/test_impl.py", "test"),
    DependencySpec("docs/adr/0001-rule.md", "decision"),
)
GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


def commit(repo: Path, path: str, text: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit {path}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for path, text in FILES.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def _build(repo: Path, specs=SPECS, *, complete: bool = True):
    return build_manifest(repo, "HEAD", specs, coverage_declared_complete=complete)


def test_build_records_present_dependencies(repo: Path) -> None:
    manifest = _build(repo)
    assert isinstance(manifest, SourceManifest)
    assert manifest.revision == _git(repo, "rev-parse", "HEAD")
    assert manifest.coverage == "complete"
    assert [d.path for d in manifest.dependencies] == sorted(s.path for s in SPECS)
    impl = next(d for d in manifest.dependencies if d.path == "src/impl.py")
    assert (impl.role, impl.status) == ("implementation", "present")
    assert impl.object_id == _git(repo, "rev-parse", "HEAD:src/impl.py")


def test_unchanged_evidence_is_current(repo: Path) -> None:
    first, second = _build(repo), _build(repo)
    assert first.digest == second.digest
    comparison = compare_manifests(first, second)
    assert (comparison.current, comparison.reason, comparison.base_changed) == (
        True,
        "unchanged",
        False,
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda spec: spec.role)
def test_single_dependency_edit_invalidates(repo: Path, spec: DependencySpec) -> None:
    before = _build(repo)
    commit(repo, spec.path, "changed\n")
    comparison = compare_manifests(before, _build(repo))
    assert (comparison.current, comparison.reason) == (False, "dependencies-changed")
    assert comparison.changed_paths == (spec.path,)


def test_unrelated_edit_with_complete_coverage_is_current(repo: Path) -> None:
    before = _build(repo)
    commit(repo, "README.md", "unrelated\n")
    comparison = compare_manifests(before, _build(repo))
    assert (comparison.current, comparison.reason, comparison.base_changed) == (
        True,
        "unchanged",
        True,
    )


def _approved_detail(manifest: SourceManifest) -> dict:
    return {
        "concern_identity": "a" * 64,
        "concern_evidence_digest": "b" * 64,
        "maintenance_consequence": "unchanged prose",
        "github_repair_revalidated": {"attestation": "ok", "manifest_digest": manifest.digest},
    }


def test_source_edit_clears_approval_with_unchanged_prose(repo: Path) -> None:
    before = _build(repo)
    detail = _approved_detail(before)
    commit(repo, "src/impl.py", "def impl():\n    return 3\n")
    retained = retain_bound_approvals(detail, compare_manifests(before, _build(repo)))
    assert "github_repair_revalidated" not in retained
    assert retained == {k: v for k, v in detail.items() if k != "github_repair_revalidated"}


def test_current_comparison_rebinds_approval(repo: Path) -> None:
    before = _build(repo)
    commit(repo, "README.md", "unrelated\n")
    after = _build(repo)
    retained = retain_bound_approvals(_approved_detail(before), compare_manifests(before, after))
    assert retained["github_repair_revalidated"] == {
        "attestation": "ok",
        "manifest_digest": after.digest,
    }


@pytest.mark.parametrize("record", [{"attestation": "ok"}, {"manifest_digest": "c" * 64}, "x"])
def test_unbound_approval_is_removed(repo: Path, record: object) -> None:
    manifest = _build(repo)
    detail = {"github_repair_revalidated": record}
    assert retain_bound_approvals(detail, compare_manifests(manifest, manifest)) == {}


@pytest.mark.parametrize(
    ("path", "setup"),
    [
        ("absent.py", None),
        ("link.py", "symlink"),
        ("src", None),
        ("../outside.py", None),
        ("/etc/passwd", None),
        ("src//impl.py", None),
        ("src/", None),
        ("./src/impl.py", None),
        ("", None),
        ("\ud800.py", None),
    ],
)
def test_unusable_dependencies_are_recorded_and_partial(
    repo: Path, path: str, setup: str | None
) -> None:
    if setup == "symlink":
        (repo / "link.py").symlink_to("src/impl.py")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "link")
    expected = "missing" if path == "absent.py" else "unsupported"
    manifest = _build(repo, (*SPECS, DependencySpec(path, "sibling")))
    assert isinstance(manifest, SourceManifest)
    recorded = next(d for d in manifest.dependencies if d.path == path)
    assert (recorded.status, recorded.object_id) == (expected, None)
    assert manifest.coverage == "partial"
    comparison = compare_manifests(manifest, manifest)
    assert (comparison.current, comparison.reason) == (False, "coverage-incomplete")


def test_deleted_dependency_is_reported_by_path(repo: Path) -> None:
    before = _build(repo)
    (repo / "src/impl.py").unlink()
    _git(repo, "commit", "-q", "-a", "-m", "delete")
    comparison = compare_manifests(before, _build(repo))
    assert (comparison.current, comparison.reason) == (False, "coverage-incomplete")
    assert comparison.changed_paths == ("src/impl.py",)


def test_symlinked_parent_is_not_followed(repo: Path) -> None:
    (repo / "alias").symlink_to("src")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "alias")
    manifest = _build(repo, (*SPECS, DependencySpec("alias/impl.py", "sibling")))
    assert isinstance(manifest, SourceManifest)
    assert next(d for d in manifest.dependencies if d.path == "alias/impl.py").status == "missing"


@pytest.mark.parametrize(
    "specs",
    [
        (DependencySpec("README.md", "test"),),
        (DependencySpec("src/impl.py", "sibling"),),
    ],
)
def test_coverage_without_implementation_is_partial(repo: Path, specs) -> None:
    assert _build(repo, specs).coverage == "partial"


def test_undeclared_coverage_is_partial(repo: Path) -> None:
    assert _build(repo, complete=False).coverage == "partial"


@pytest.mark.parametrize(
    "case",
    [
        "bad-revision",
        "nul-revision",
        "not-a-repo",
        "unknown-role",
        "non-str-path",
        "duplicate-path",
        "too-many",
        "oversize-path",
        "subdirectory-root",
    ],
)
def test_unbindable_analysis_is_unknown(repo: Path, tmp_path: Path, case: str) -> None:
    root, revision, specs = repo, "HEAD", SPECS
    if case == "bad-revision":
        revision = "no-such-branch"
    elif case == "nul-revision":
        revision = "HEAD\0x"
    elif case == "non-str-path":
        specs = (*SPECS, DependencySpec(5, "sibling"))  # type: ignore[arg-type]
    elif case == "oversize-path":
        specs = (*SPECS, DependencySpec("a" * (MAX_PATH_BYTES + 1), "sibling"))
    elif case == "subdirectory-root":
        root = repo / "src"
    elif case == "not-a-repo":
        root = tmp_path / "empty"
        root.mkdir()
    elif case == "unknown-role":
        specs = (DependencySpec("src/impl.py", "owner"),)
    elif case == "duplicate-path":
        specs = (*SPECS, DependencySpec("src/impl.py", "test"))
    else:
        specs = tuple(DependencySpec(f"f{n}.py", "sibling") for n in range(MAX_DEPENDENCIES + 1))
    result = build_manifest(root, revision, specs, coverage_declared_complete=True)
    assert isinstance(result, AnalysisUnknown)
    good = _build(repo)
    for pair in ((result, good), (good, result)):
        comparison = compare_manifests(*pair)
        assert (comparison.current, comparison.reason) == (False, "unknown")


def test_option_like_revision_is_not_an_option(repo: Path) -> None:
    result = build_manifest(repo, "--all", SPECS, coverage_declared_complete=True)
    assert isinstance(result, AnalysisUnknown)


def test_pathspec_magic_is_literal(repo: Path) -> None:
    for path in (":(glob)src/*.py", "src/*.py"):
        manifest = _build(repo, (*SPECS, DependencySpec(path, "sibling")))
        assert next(d for d in manifest.dependencies if d.path == path).status == "missing"


def test_inherited_git_dir_is_ignored(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    manifest = _build(repo)
    assert isinstance(manifest, SourceManifest)
    assert manifest.coverage == "complete"


def test_record_round_trips(repo: Path) -> None:
    manifest = _build(repo, (*SPECS, DependencySpec("absent.py", "test")))
    parsed = manifest_from_record(manifest.as_record())
    assert parsed == manifest
    assert parsed.digest == manifest.digest


def _mutated(manifest: SourceManifest, change) -> dict:
    record = manifest.as_record()
    change(record)
    return record


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(schema="other"),
        lambda r: r.update(revision="HEAD"),
        lambda r: r.update(coverage="unknown"),
        lambda r: r["dependencies"][0].update(status="stale"),
        lambda r: r["dependencies"][0].update(object_id=None),
        lambda r: r["dependencies"][0].update(role="owner"),
        lambda r: r["dependencies"].reverse(),
        lambda r: r["dependencies"].append(dict(r["dependencies"][-1])),
        lambda r: r["dependencies"][0].update(status="missing", object_id=None),
        lambda r: r.pop("coverage"),
        lambda r: r["dependencies"][0].update(path="../../etc/passwd"),
        lambda r: r["dependencies"][0].update(path="/" + "a" * MAX_PATH_BYTES),
        lambda r: (r.update(coverage="partial"), r["dependencies"][0].update(
            path="/" + "a" * MAX_PATH_BYTES, status="unsupported", object_id=None
        )),
    ],
)
def test_malformed_record_is_unknown(repo: Path, change) -> None:
    record = _mutated(_build(repo), change)
    assert isinstance(manifest_from_record(record), AnalysisUnknown)


@pytest.mark.parametrize(
    "record", [None, "manifest", [], {"schema": "desloppify-source-manifest:v1"}]
)
def test_non_record_is_unknown(record: object) -> None:
    assert isinstance(manifest_from_record(record), AnalysisUnknown)
