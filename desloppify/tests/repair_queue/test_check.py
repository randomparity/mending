"""Bounded evidence-anchor check over a fixture repository."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import desloppify.engine.repair_check as repair_check
from desloppify.engine.repair_check import CheckResult, check_concern, passing_check
from desloppify.engine.repair_manifest import SourceCheckout, SourceManifest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "impl.py").write_text("def load_root():\n    return find_root()\n")
    (root / "src" / "sibling.py").write_text("def sibling():\n    return 2\n")
    (root / "src" / "pkg" / "sibling.py").write_text("x = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def _issue(*evidence, related=("src/impl.py", "src/sibling.py")) -> dict:
    return {
        "file": "src/impl.py",
        "detail": {"related_files": list(related), "evidence": list(evidence)},
    }


def _check(repo: Path, issue: dict) -> CheckResult:
    manifest = SourceCheckout(repo, "HEAD").manifest_for(issue)
    assert isinstance(manifest, SourceManifest)
    return check_concern(repo, manifest, issue)


def test_cited_line_passes(repo: Path) -> None:
    result = _check(repo, _issue("impl.py:2 calls `find_root` again", "no citation here"))
    assert (result.outcome, result.reason) == ("pass", "evidence anchors hold")
    record = result.as_record()
    assert record["claims"] == [
        {"citations": [{"path": "src/impl.py", "start": 2, "end": 2}], "identifiers": ["find_root"]}
    ]
    assert record["bounds"]["seconds"] == repair_check.MAX_SECONDS
    assert passing_check(record, result.digest)


@pytest.mark.parametrize(
    "evidence", ["impl.py:3 re-derives the root", "src/impl.py:2-1 is reversed", "impl.py:0 x"]
)
def test_line_outside_file_fails(repo: Path, evidence: str) -> None:
    result = _check(repo, _issue(evidence))
    assert (result.outcome, result.reason) == ("fail", "cited line is outside the file")


def test_absent_identifier_fails(repo: Path) -> None:
    result = _check(repo, _issue("impl.py:1 calls `Loader.resolve()`"))
    assert (result.outcome, result.reason) == (
        "fail", "quoted identifier is absent from the cited files"
    )


def test_backticked_citation_and_path_are_not_quotes(repo: Path) -> None:
    result = _check(repo, _issue("`src/impl.py:1` and `impl.py` re-derive `load_root`"))
    assert result.outcome == "pass"
    assert result.as_record()["claims"][0]["identifiers"] == ["load_root"]


def test_no_citation_is_unknown(repo: Path) -> None:
    result = _check(repo, _issue("the loader re-derives the root"))
    assert (result.outcome, result.transient) == ("unknown", False)
    assert not passing_check(result.as_record(), result.digest)


def test_unresolved_token_is_ignored(repo: Path) -> None:
    assert _check(repo, _issue("since v1.2:3, impl.py:1 re-derives")).outcome == "pass"
    assert _check(repo, _issue("since v1.2:3 and host.example:8080")).outcome == "unknown"


def test_suffix_resolution_and_ambiguity(repo: Path) -> None:
    related = ("src/impl.py", "src/sibling.py", "src/pkg/sibling.py")
    assert _check(repo, _issue("pkg/sibling.py:1 sets `x`", related=related)).outcome == "pass"
    ambiguous = _check(repo, _issue("sibling.py:1 sets it", related=related))
    assert ambiguous.outcome == "unknown"


def test_blob_over_bound_is_unknown(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(repair_check, "MAX_FILE_BYTES", 4)
    result = _check(repo, _issue("impl.py:1 re-derives"))
    assert (result.outcome, result.transient) == ("unknown", False)


def test_total_bytes_over_bound_is_unknown(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(repair_check, "MAX_TOTAL_BYTES", 50)
    result = _check(repo, _issue("impl.py:1 and sibling.py:1 drift"))
    assert (result.outcome, result.transient) == ("unknown", False)


def test_too_many_items_is_unknown(repo: Path) -> None:
    items = ["impl.py:1 x"] * (repair_check.MAX_EVIDENCE_ITEMS + 1)
    assert _check(repo, _issue(*items)).outcome == "unknown"


def test_too_many_evidence_bytes_is_unknown(repo: Path) -> None:
    item = "impl.py:1 " + "x" * repair_check.MAX_EVIDENCE_BYTES
    result = _check(repo, _issue(item))
    assert (result.outcome, result.reason) == ("unknown", "evidence exceeds its bound")


def test_identifier_must_be_a_whole_word(repo: Path) -> None:
    assert _check(repo, _issue("impl.py:1 calls `load`")).outcome == "fail"


def test_too_many_citations_is_unknown(repo: Path) -> None:
    item = " ".join(["impl.py:1"] * (repair_check.MAX_CITATIONS + 1))
    assert _check(repo, _issue(item)).outcome == "unknown"


def test_non_string_evidence_is_unknown(repo: Path) -> None:
    issue = _issue()
    manifest = SourceCheckout(repo, "HEAD").manifest_for(issue)
    issue["detail"]["evidence"] = ["impl.py:1", 3]
    assert check_concern(repo, manifest, issue).outcome == "unknown"


def test_deadline_is_transient_unknown(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(repair_check, "MAX_SECONDS", 0)
    result = _check(repo, _issue("impl.py:1 re-derives"))
    assert (result.outcome, result.reason, result.transient) == (
        "unknown", "time bound exceeded", True
    )


def test_unreadable_blob_is_transient_unknown(repo: Path, monkeypatch) -> None:
    monkeypatch.setattr(repair_check, "read_blob", lambda *args, **kwargs: None)
    result = _check(repo, _issue("impl.py:1 re-derives"))
    assert (result.outcome, result.transient) == ("unknown", True)


def test_digest_ignores_reason_and_bounds(repo: Path) -> None:
    result = _check(repo, _issue("impl.py:1 re-derives"))
    record = {**result.as_record(), "reason": "reworded", "bounds": {"seconds": 99}}
    assert passing_check(record, result.digest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r, d: (None, d),
        lambda r, d: ({**r, "schema": "other"}, d),
        lambda r, d: ({**r, "outcome": "fail"}, d),
        lambda r, d: ({k: v for k, v in r.items() if k != "claims"}, d),
        lambda r, d: (r, "0" * 64),
        lambda r, d: ({**r, "claims": [object()]}, d),
    ],
)
def test_passing_check_requires_digest_and_pass(repo: Path, mutate) -> None:
    result = _check(repo, _issue("impl.py:1 re-derives"))
    assert not passing_check(*mutate(result.as_record(), result.digest))
