"""One-per-run repair selection, ranking, and the reviewed brief version (ADR 0014)."""

from __future__ import annotations

import argparse
import copy
import json
from types import SimpleNamespace

import pytest

from desloppify.app.commands.repair_queue import _create_once, cmd_repair_queue
from desloppify.engine.repair_brief import reviewed_version
from desloppify.engine.repair_check import CheckResult, Citation, Claim
from desloppify.engine.repair_manifest import MANIFEST_SCHEMA, manifest_from_record
from desloppify.engine.repair_queue import (
    GitHubIssue,
    PromotionCandidate,
    classify,
    concern_key,
    finding_key,
    item_hashes,
)
from desloppify.engine.repair_selection import rank_key

REPOSITORY = "owner/repository"


def _manifest(paths: tuple[str, ...], blob: str = "d" * 40):
    """The first path is the implementation; records list dependencies sorted by path."""
    return manifest_from_record({
        "schema": MANIFEST_SCHEMA, "revision": "c" * 40, "coverage": "complete",
        "dependencies": [
            {
                "path": path, "role": "implementation" if path == paths[0] else "sibling",
                "status": "present", "object_id": blob,
            }
            for path in sorted(paths)
        ],
    })


def _check(*spans: tuple[int, int], path: str = "src/a.py") -> CheckResult:
    claims = tuple(Claim((Citation(path, start, end),), ()) for start, end in spans)
    return CheckResult("pass", "evidence anchors hold", claims)


def _revalidate(issue: dict, check: CheckResult, blob: str = "d" * 40) -> dict:
    route, identity, evidence = item_hashes(issue)
    key = (finding_key if route == "finding" else concern_key)(REPOSITORY, identity)
    paths = (issue["file"], *issue["detail"].get("related_files", []))
    manifest = _manifest(tuple(dict.fromkeys(paths)), blob)
    issue["detail"]["github_repair_revalidated"] = {
        "key": key, "repository": REPOSITORY, "evidence_digest": evidence,
        "manifest": manifest.as_record(), "manifest_digest": manifest.digest,
        "check": check.as_record(), "check_digest": check.digest,
    }
    return issue


def _concern(
    name: str, digit: str, *, related: tuple[str, ...] = (), spans=((3, 3),), blob="d" * 40
) -> dict:
    issue = {
        "id": f"concerns::{name}", "detector": "concerns", "status": "open",
        "file": f"src/{name}.py", "confidence": "high", "summary": f"{name} duplicates policy",
        "detail": {
            "concern_identity": digit * 64, "concern_evidence_digest": "b" * 64,
            "related_files": list(related),
            "maintenance_consequence": "Two policies drift",
            "evidence": [f"{name}.py:3 re-derives the root"],
            "proposed_owner": "the loader module",
            "protected_contracts": ["CLI exit codes"],
            "verification": "Run the loader tests",
        },
    }
    return _revalidate(issue, _check(*spans, path=f"src/{name}.py"), blob)


def _dupe() -> dict:
    def function(line: int) -> dict:
        return {"file": "/abs/src/d.py", "name": "save", "line": line, "loc": 4}

    issue = {
        "id": "dupes::src/d.py::save", "detector": "dupes", "status": "open",
        "file": "src/d.py", "confidence": "high", "summary": "Exact dupe: save <-> save",
        "detail": {
            "fn_a": function(1), "fn_b": function(10), "kind": "exact", "cluster_size": 2,
        },
    }
    return _revalidate(issue, _check((1, 4), (10, 13), path="src/d.py"))


def _candidate(issue: dict) -> PromotionCandidate:
    candidate = classify(issue, REPOSITORY).candidate
    assert candidate is not None
    return candidate


def test_reviewed_version_ignores_wording_and_tracks_material() -> None:
    issue = _concern("alpha", "1")
    version = reviewed_version(issue, _candidate(issue))
    assert version is not None and len(version) == 64

    reworded = copy.deepcopy(issue)
    reworded["summary"] = "Alpha re-derives the loader policy"
    assert reviewed_version(reworded, _candidate(reworded)) == version

    contracts = copy.deepcopy(issue)
    contracts["detail"]["protected_contracts"] = ["CLI exit codes", "Config schema"]
    assert reviewed_version(contracts, _candidate(contracts)) != version

    blob = _concern("alpha", "1", blob="e" * 40)
    assert reviewed_version(blob, _candidate(blob)) != version

    parked = copy.deepcopy(issue)
    parked["detail"]["proposed_owner"] = "see https://example.com/x"
    assert reviewed_version(parked, _candidate(parked)) is None


def test_rank_key_orders_risk_feasibility_evidence_benefit() -> None:
    def key(issue: dict) -> tuple:
        return rank_key(issue, _candidate(issue))

    wide = _concern("wide", "1", related=("src/peer.py",), spans=((1, 9), (2, 9), (3, 9)))
    narrow = _concern("narrow", "2")
    assert key(narrow) < key(wide)  # fewer files first (risk)
    assert key(_dupe()) < key(narrow)  # mechanical verification first (feasibility)
    cited = _concern("cited", "3", spans=((3, 3), (5, 5)))
    assert key(cited) < key(narrow)  # more checked citations (evidence)
    longer = _concern("longer", "4", spans=((3, 6),))
    assert key(longer) < key(narrow)  # more cited lines (benefit)
    twin = _concern("twin", "5")
    assert (key(narrow) < key(twin)) == (_candidate(narrow).key < _candidate(twin).key)


class _Source:
    """Fake checkout whose files still match each item's stored manifest."""

    def manifest_for(self, issue: dict):
        return manifest_from_record(issue["detail"]["github_repair_revalidated"]["manifest"])


def _stored_check(issue: dict, _manifest) -> SimpleNamespace:
    digest = issue["detail"]["github_repair_revalidated"]["check_digest"]
    return SimpleNamespace(outcome="pass", reason="", transient=False, digest=digest)


class _GitHub:
    def __init__(self, *issues: GitHubIssue, fail_create: bool = False) -> None:
        self.issues = list(issues)
        self.creates: list[str] = []
        self.fail_create = fail_create

    def search(self, repository: str, term: str) -> list[GitHubIssue]:
        return [issue for issue in self.issues if term in issue.body]

    def view(self, repository: str, number: int) -> GitHubIssue:
        return next(issue for issue in self.issues if issue.number == number)

    def create(self, repository: str, title: str, body: str, *, ready: bool) -> None:
        self.creates.append(body)
        if self.fail_create:
            raise RuntimeError("unavailable")
        number = 100 + len(self.creates)
        self.issues.append(GitHubIssue(number, f"https://example.test/{number}", "open", body))


def _state(*items: dict) -> dict:
    return {"work_items": {item["id"]: item for item in items}}


def _args(state: dict, client: _GitHub, *, apply: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        command="repair-queue", repair_queue_action="sync", repository=REPOSITORY,
        state=None, apply=apply, issue_id=None, marker=None, client=client,
        source=_Source(), check=_stored_check, state_data=state,
    )


def _sync(state: dict, client: _GitHub, *, apply: bool = True) -> None:
    cmd_repair_queue(_args(state, client, apply=apply))


def _selection(state: dict) -> dict:
    return state["repair_queue_selection"][REPOSITORY]


def _link(issue: dict, number: int, state: str) -> dict:
    candidate = _candidate(issue)
    return {
        "key": candidate.key, "repository": REPOSITORY,
        "evidence_digest": candidate.evidence_digest,
        "number": number, "url": f"https://example.test/{number}", "state": state,
    }


def test_three_candidates_create_only_the_rank_first(capsys) -> None:
    wide = _concern("wide", "1", related=("src/peer.py",))
    narrow, dupe = _concern("narrow", "2"), _dupe()
    state, client = _state(wide, narrow, dupe), _GitHub()

    _sync(state, client)

    assert len(client.creates) == 1
    assert f"<!-- desloppify-finding-key: {_candidate(dupe).key} -->" in client.creates[0]
    assert dupe["detail"]["github_repair"]["number"] == 101
    assert len(dupe["detail"]["github_repair"]["reviewed_brief_version"]) == 64
    assert "github_repair" not in narrow["detail"] and "github_repair" not in wide["detail"]
    out = capsys.readouterr().out
    assert f"Deferred {narrow['id']}: {dupe['id']} ranked first." in out
    assert f"Deferred {wide['id']}: {dupe['id']} ranked first." in out
    assert _selection(state)["outcome"] == "selected"
    assert _selection(state)["issue_id"] == dupe["id"]


def test_clean_state_records_no_op_and_dry_run_records_nothing(capsys) -> None:
    dry = _state()
    _sync(dry, _GitHub(), apply=False)
    assert "repair_queue_selection" not in dry

    empty = _state()
    _sync(empty, _GitHub())
    assert "repair_queue_selection" not in empty  # never rewrite a state that loaded empty

    fixed = _concern("fixed", "1")
    fixed["status"] = "fixed"
    state = _state(fixed)
    _sync(state, _GitHub())
    selection = _selection(state)
    assert (selection["outcome"], selection["reason"], selection["issue_id"]) == (
        "no-op", "no safe small repair", None
    )
    assert selection["at"]
    assert "No repair selected: no safe small repair." in capsys.readouterr().out


def test_pending_repair_blocks_selection(capsys) -> None:
    narrow = _concern("narrow", "2")
    gone = _concern("gone", "3")
    gone["status"] = "fixed"
    gone["detail"]["github_repair_pending"] = {
        "key": "f" * 64, "repository": REPOSITORY, "evidence_digest": "b" * 64
    }
    odd = _concern("odd", "4")
    odd["status"] = "fixed"
    odd["detail"]["github_repair_pending"] = "garbage"
    state, client = _state(narrow, gone, odd), _GitHub()

    _sync(state, client)

    assert client.creates == []
    assert _selection(state)["reason"] == "unresolved repair publication"
    out = capsys.readouterr().out
    assert (
        f"repair publication for {gone['id']} is unresolved; check GitHub, then run"
        f" repair-queue recover {'f' * 64}." in out
    )
    assert f"the pending repair record on {odd['id']} is unrecognized" in out


def test_create_once_refuses_while_another_repair_is_pending() -> None:
    narrow = _concern("narrow", "2")
    gone = _concern("gone", "3")
    gone["status"] = "fixed"
    gone["detail"]["github_repair_pending"] = {
        "key": "f" * 64, "repository": REPOSITORY, "evidence_digest": "b" * 64
    }
    state, client = _state(narrow, gone), _GitHub()

    assert _create_once(_args(state, client), client, _candidate(narrow)) is False
    assert client.creates == []
    assert "github_repair_pending" not in narrow["detail"]


def test_human_edited_and_closed_links_are_kept_and_another_is_selected() -> None:
    edited, closed = _concern("edited", "1"), _concern("closed", "2")
    matched, fresh = _concern("matched", "3"), _concern("fresh", "4")
    edited["detail"]["github_repair"] = _link(edited, 7, "open")
    closed["detail"]["github_repair"] = _link(closed, 8, "open")
    marker = f"<!-- desloppify-concern-key: {_candidate(matched).key} -->"
    client = _GitHub(
        GitHubIssue(7, "https://example.test/7", "open", "operator rewrote this body"),
        GitHubIssue(8, "https://example.test/8", "closed", "closed as not planned"),
        GitHubIssue(9, "https://example.test/9", "closed", marker),
    )
    state = _state(edited, closed, matched, fresh)

    _sync(state, client)

    assert len(client.creates) == 1
    assert fresh["detail"]["github_repair"]["number"] == 101
    assert edited["detail"]["github_repair"]["number"] == 7
    assert closed["detail"]["github_repair"]["state"] == "closed"
    assert matched["detail"]["github_repair"]["number"] == 9
    for item in (edited, closed, matched):
        assert item["detail"]["github_repair"]["reviewed_brief_version"] == reviewed_version(
            item, _candidate(item)
        )


def test_dismissed_item_is_never_selected() -> None:
    dismissed = _concern("dismissed", "1")
    dismissed["status"] = "wontfix"
    state, client = _state(dismissed), _GitHub()

    _sync(state, client)

    assert client.creates == []
    assert _selection(state)["outcome"] == "no-op"


def test_uncertain_create_keeps_pending_and_next_run_creates_nothing() -> None:
    narrow = _concern("narrow", "2")
    state, client = _state(narrow), _GitHub(fail_create=True)

    _sync(state, client)
    assert len(client.creates) == 1
    assert narrow["detail"]["github_repair_pending"]["key"] == _candidate(narrow).key
    assert _selection(state)["outcome"] == "selected"

    _sync(state, client)
    assert len(client.creates) == 1
    assert _selection(state)["reason"] == "unresolved repair publication"


def test_material_change_reports_changed_and_wording_change_does_not(capsys) -> None:
    narrow = _concern("narrow", "2")
    state, client = _state(narrow), _GitHub()
    _sync(state, client)
    version = narrow["detail"]["github_repair"]["reviewed_brief_version"]
    capsys.readouterr()

    narrow["summary"] = "Narrow re-derives the loader policy"
    _sync(state, client)
    assert narrow["detail"]["github_repair"]["reviewed_brief_version"] == version
    out = capsys.readouterr().out
    assert f"Linked {narrow['id']} to issue #101." in out and "Changed" not in out

    narrow["detail"]["protected_contracts"] = ["CLI exit codes", "Config schema"]
    _sync(state, client)
    changed = narrow["detail"]["github_repair"]["reviewed_brief_version"]
    assert changed != version
    assert f"Changed {narrow['id']}: issue #101 evidence or reviewed brief changed" in (
        capsys.readouterr().out
    )

    narrow["detail"]["concern_evidence_digest"] = "c" * 64
    _revalidate(narrow, _check((3, 3), path="src/narrow.py"))
    _sync(state, client)
    assert narrow["detail"]["github_repair"]["evidence_digest"] == "c" * 64
    assert f"Changed {narrow['id']}" in capsys.readouterr().out
    assert len(client.creates) == 1


@pytest.mark.parametrize(
    "content",
    [{"version": 2, "work_items": {}}, {"version": 2, "work_items": {"a": {"id": "b"}}}],
    ids=["empty", "invalid-invariants"],
)
def test_sync_leaves_a_state_file_without_work_items_untouched(tmp_path, content) -> None:
    state_file = tmp_path / "state.json"
    original = json.dumps(content).encode()
    state_file.write_bytes(original)
    args = _args({}, _GitHub())
    args.state_data, args.state = None, str(state_file)

    cmd_repair_queue(args)

    assert state_file.read_bytes() == original
    assert not state_file.with_suffix(".json.bak").exists()
