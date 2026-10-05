"""End-to-end repair-queue sync matrix over a local fixture repository."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from desloppify.app.commands.repair_queue import cmd_repair_queue
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.merge_issues import upsert_issues
from desloppify.engine.repair_manifest import SourceCheckout
from desloppify.engine.repair_queue import GitHubIssue, concern_key

IDENTITY = "a" * 64
EVIDENCE = "b" * 64
REPOSITORY = "owner/repository"
KEY = concern_key(REPOSITORY, IDENTITY)
KEY_BODY = f"## Repair queue record\n\n<!-- desloppify-concern-key: {KEY} -->\n"
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


def _commit(repo: Path, path: str, text: str) -> None:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit {path}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _commit(root, "src/impl.py", "def impl():\n    return 1\n")
    _commit(root, "src/sibling.py", "def sibling():\n    return 2\n")
    _commit(root, "README.md", "readme\n")
    return root


class _GitHub:
    """Injected gh responses: search by term, view by number, counted creates."""

    def __init__(self, on_search=None) -> None:
        self.issues: list[GitHubIssue] = []
        self.creates = 0
        self.searches = 0
        self.on_search = on_search

    def resolve_repository(self, repository: str) -> str:
        return repository

    def search(self, repository: str, term: str) -> list[GitHubIssue]:
        self.searches += 1
        if self.on_search is not None:
            self.on_search()
        return [issue for issue in self.issues if term in issue.body]

    def view(self, repository: str, number: int) -> GitHubIssue:
        return next(issue for issue in self.issues if issue.number == number)

    def create(self, repository: str, title: str, body: str) -> None:
        self.creates += 1
        number = 100 + self.creates
        self.issues.append(GitHubIssue(number, f"https://example.test/{number}", "open", body))


def _state() -> dict:
    item = {
        "id": "concerns::src/impl.py::layering",
        "detector": "concerns",
        "status": "open",
        "file": "src/impl.py",
        "tier": 2,
        "confidence": "high",
        "summary": "Parser duplicates the loader policy",
        "suppressed": False,
        "detail": {
            "concern_identity": IDENTITY,
            "concern_evidence_digest": EVIDENCE,
            "related_files": ["src/impl.py", "src/sibling.py"],
            "maintenance_consequence": "Two policies drift",
            "evidence": ["impl.py:1 re-derives the root"],
            "proposed_owner": "the loader module",
            "protected_contracts": ["CLI exit codes"],
            "verification": "Run the loader tests",
        },
    }
    return {"work_items": {item["id"]: item}}


def _run(action: str, state: dict, repo: Path, client: _GitHub, **extra) -> None:
    values = {
        "command": "repair-queue", "repair_queue_action": action, "repository": REPOSITORY,
        "state": None, "apply": True, "issue_id": None, "marker": None, "client": client,
        "state_data": state, "source": SourceCheckout(repo, "HEAD"),
    }
    cmd_repair_queue(argparse.Namespace(**{**values, **extra}))


def _detail(state: dict) -> dict:
    return next(iter(state["work_items"].values()))["detail"]


def _revalidated(repo: Path, client: _GitHub) -> dict:
    state = _state()
    _run("revalidate", state, repo, client, issue_id=next(iter(state["work_items"])))
    return state


def test_base_move_without_dependency_change_creates_once(repo: Path) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    _commit(repo, "README.md", "unrelated\n")
    _run("sync", state, repo, client)
    _run("sync", state, repo, client)
    assert client.creates == 1
    assert _detail(state)["github_repair"]["number"] == 101
    assert "github_repair_revalidated" in _detail(state)


@pytest.mark.parametrize("path", ["src/impl.py", "src/sibling.py"])
def test_dependency_change_before_dispatch_clears_and_skips(repo: Path, path: str) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    _commit(repo, path, "changed = True\n")
    _run("sync", state, repo, client)
    assert (client.searches, client.creates) == (0, 0)
    assert "github_repair_revalidated" not in _detail(state)
    assert "github_repair_pending" not in _detail(state)


def test_dependency_change_during_github_reads_blocks_create(repo: Path) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    client.on_search = lambda: _commit(repo, "src/impl.py", f"raced = {client.searches}\n")
    _run("sync", state, repo, client)
    assert client.searches > 0
    assert client.creates == 0
    assert "github_repair_pending" not in _detail(state)
    assert "github_repair_revalidated" not in _detail(state)


def test_closed_link_survives_dependency_change(repo: Path) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    _run("sync", state, repo, client)
    client.issues[0] = GitHubIssue(101, "https://example.test/101", "closed", KEY_BODY)
    _run("sync", state, repo, client)
    link = dict(_detail(state)["github_repair"])
    assert link["state"] == "closed"
    _commit(repo, "src/sibling.py", "changed = True\n")
    _run("sync", state, repo, client)
    assert _detail(state)["github_repair"] == link
    assert "github_repair_revalidated" not in _detail(state)
    assert client.creates == 1


def test_rescan_keeps_manifest_binding(repo: Path) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    record = dict(_detail(state)["github_repair_revalidated"])
    item = next(iter(state["work_items"].values()))
    scanned = {**item, "detail": {k: v for k, v in item["detail"].items() if not k.startswith("github_")}}
    upsert_issues(state["work_items"], [scanned], [], "2026-10-05T00:00:00Z", lang=None)
    assert _detail(state)["github_repair_revalidated"] == record
    _run("sync", state, repo, client)
    assert client.creates == 1


def test_unverified_search_hit_is_not_adopted(repo: Path) -> None:
    client = _GitHub()
    client.issues.append(GitHubIssue(7, "https://example.test/7", "open", f"pasted {KEY}"))
    state = _revalidated(repo, client)
    _run("sync", state, repo, client)
    assert client.creates == 0
    assert "github_repair" not in _detail(state)


def test_dependency_change_during_create_keeps_pending_then_adopts(repo: Path) -> None:
    class RacingCreate(_GitHub):
        def create(self, repository: str, title: str, body: str) -> None:
            super().create(repository, title, body)
            _commit(repo, "src/impl.py", "raced = True\n")

    client = RacingCreate()
    state = _revalidated(repo, client)
    _run("sync", state, repo, client)
    assert client.creates == 1
    assert "github_repair" not in _detail(state)
    assert "github_repair_pending" in _detail(state)
    assert "github_repair_revalidated" not in _detail(state)
    _run("revalidate", state, repo, client, issue_id=next(iter(state["work_items"])))
    _run("sync", state, repo, client)
    assert client.creates == 1
    assert _detail(state)["github_repair"]["number"] == 101
    assert "github_repair_pending" not in _detail(state)


def test_revalidate_names_unbound_dependency(repo: Path) -> None:
    state = _state()
    _detail(state)["related_files"] = ["./src/sibling.py"]
    with pytest.raises(CommandError, match=r"\./src/sibling\.py is unsupported"):
        _run("revalidate", state, repo, _GitHub(), issue_id=next(iter(state["work_items"])))
    assert "github_repair_revalidated" not in _detail(state)


def test_mistyped_revision_keeps_revalidation(repo: Path) -> None:
    client = _GitHub()
    state = _revalidated(repo, client)
    _run("sync", state, repo, client, source=SourceCheckout(repo, "HAED"))
    assert (client.searches, client.creates) == (0, 0)
    assert "github_repair_revalidated" in _detail(state)
