"""Typed repair-candidate classification over the concern and finding routes (#21)."""

from __future__ import annotations

import copy
import subprocess

import pytest

from desloppify.engine._state.merge_issues import upsert_issues
from desloppify.engine.repair_check import CheckResult
from desloppify.engine.repair_manifest import MANIFEST_SCHEMA, manifest_from_record
from desloppify.engine.repair_queue import (
    FINDING_KEY_LINE,
    KEY_LINE,
    PROPOSAL_LANE,
    PROPOSAL_LINE,
    REPAIR_LANE,
    GitHubIssueClient,
    RepairRecordError,
    carries_concern_marker,
    classify,
    concern_failures,
    concern_key,
    finding_key,
    item_hashes,
    lane_for,
    normalize_record,
)

REPOSITORY = "owner/repository"
IDENTITY = "a" * 64
EVIDENCE = "b" * 64
MANIFEST = {
    "schema": MANIFEST_SCHEMA,
    "revision": "c" * 40,
    "coverage": "complete",
    "dependencies": [
        {"path": "src/impl.py", "role": "implementation", "status": "present", "object_id": "d" * 40}
    ],
}
PASS = CheckResult("pass", "evidence anchors hold", ())


def _bound(key: str, evidence: str) -> dict:
    return {
        "key": key, "repository": REPOSITORY, "evidence_digest": evidence,
        "manifest": MANIFEST, "manifest_digest": manifest_from_record(MANIFEST).digest,
        "check": PASS.as_record(), "check_digest": PASS.digest,
    }


def _concern(*, related=("src/impl.py", "src/peer.py"), confidence="high") -> dict:
    return {
        "id": "concerns::item", "detector": "concerns", "status": "open",
        "file": "src/impl.py", "confidence": confidence,
        "detail": {
            "concern_identity": IDENTITY, "concern_evidence_digest": EVIDENCE,
            "related_files": list(related), "verification": "Run the loader tests",
            "github_repair_revalidated": _bound(concern_key(REPOSITORY, IDENTITY), EVIDENCE),
        },
    }


def _function(name: str, line: int, file: str = "src/impl.py") -> dict:
    return {"file": file, "name": name, "line": line, "loc": 12}


def _dupe(**detail) -> dict:
    issue = {
        "id": "dupes::src/impl.py::alpha::src/impl.py::beta", "detector": "dupes",
        "status": "open", "file": "src/impl.py", "confidence": "high", "tier": 2,
        "summary": "Exact dupe: alpha <-> beta",
        "detail": {
            "fn_a": _function("alpha", 1), "fn_b": _function("beta", 20),
            "kind": "exact", "similarity": 1.0, "cluster_size": 2, **detail,
        },
    }
    route, identity, evidence = item_hashes(issue) or ("", "", "")
    if route:
        issue["detail"]["github_repair_revalidated"] = _bound(
            finding_key(REPOSITORY, identity), evidence
        )
    return issue


def test_exact_same_file_dupe_is_a_small_finding_repair() -> None:
    result = classify(_dupe(), REPOSITORY)
    assert result.kind == "small_repair"
    candidate = result.candidate
    assert candidate is not None and candidate.route == "finding"
    assert candidate.key == finding_key(REPOSITORY, candidate.identity)
    assert candidate.key != concern_key(REPOSITORY, candidate.identity)
    assert lane_for(candidate) == REPAIR_LANE


def test_bounded_high_confidence_concern_is_a_small_repair() -> None:
    result = classify(_concern(), REPOSITORY)
    assert (result.kind, result.reason) == ("small_repair", "within the small-repair bound")
    assert result.candidate is not None and result.candidate.route == "concern"


def test_cross_boundary_uncertain_concern_is_a_proposal() -> None:
    issue = _concern(related=("src/impl.py", "lib/other.py"), confidence="medium")
    result = classify(issue, REPOSITORY)
    assert result.kind == "proposal"
    assert concern_failures(issue) == (
        "files span 2 directories", "review confidence is not high",
    )
    assert result.candidate is not None and lane_for(result.candidate) == PROPOSAL_LANE


def test_concern_scope_and_verification_predicates() -> None:
    issue = _concern(related=("src/a.py", "src/b.py", "src/c.py"))
    issue["detail"]["verification"] = "  "
    assert concern_failures(issue) == (
        "scope spans 4 files; the bound is 3", "no verification plan",
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"fn_a": {"file": "src/impl.py", "name": "alpha"}}, "finding evidence is malformed"),
        ({"fn_b": {**_function("beta", 20), "line": True}}, "finding evidence is malformed"),
        ({"fn_b": _function("beta", 20, file="src/other.py")}, "finding exceeds the small-repair bound"),
        ({"kind": "near"}, "finding exceeds the small-repair bound"),
        ({"cluster_size": 3}, "finding exceeds the small-repair bound"),
    ],
    ids=["missing-line", "bool-line", "cross-file", "near", "cluster"],
)
def test_insufficient_or_broad_findings_are_ineligible(change: dict, reason: str) -> None:
    result = classify(_dupe(**change), REPOSITORY)
    assert (result.kind, result.reason, result.candidate) == ("ineligible", reason, None)


@pytest.mark.parametrize("detector", ["unused", "structural", "security", "orphaned"])
def test_other_detectors_have_no_route(detector: str) -> None:
    issue = {**_dupe(), "detector": detector}
    result = classify(issue, REPOSITORY)
    assert (result.kind, result.reason) == ("ineligible", "detector has no repair route")


def test_finding_without_revalidation_is_ineligible() -> None:
    issue = _dupe()
    del issue["detail"]["github_repair_revalidated"]
    assert classify(issue, REPOSITORY).reason == "no current source-bound revalidation"
    assert classify(issue, REPOSITORY, require_revalidation=False).kind == "small_repair"


def test_finding_identity_survives_a_line_move_but_evidence_does_not() -> None:
    before = item_hashes(_dupe())
    after = item_hashes(_dupe(fn_b=_function("beta", 30)))
    assert before is not None and after is not None
    assert before[1] == after[1] and before[2] != after[2]


def test_finding_records_have_no_legacy_marker() -> None:
    with pytest.raises(RepairRecordError):
        normalize_record(
            "github_repair", {"marker": "e" * 64, "repository": REPOSITORY},
            REPOSITORY, IDENTITY, EVIDENCE, route="finding",
        )


def test_each_lane_accepts_only_its_own_body_line() -> None:
    proposal = classify(_concern(confidence="low"), REPOSITORY).candidate
    repair = classify(_concern(), REPOSITORY).candidate
    finding = classify(_dupe(), REPOSITORY).candidate
    assert proposal is not None and repair is not None and finding is not None
    assert carries_concern_marker(PROPOSAL_LINE.format(proposal.key), proposal)
    assert not carries_concern_marker(KEY_LINE.format(proposal.key), proposal)
    assert not carries_concern_marker(PROPOSAL_LINE.format(repair.key), repair)
    assert carries_concern_marker(FINDING_KEY_LINE.format(finding.key), finding)
    assert not carries_concern_marker(KEY_LINE.format(finding.key), finding)


def test_proposal_create_carries_no_label() -> None:
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    GitHubIssueClient(run).create(REPOSITORY, "Proposal: x", "body", ready=False)
    assert "--label" not in calls[0] and "status:ready" not in calls[0]


def test_scan_merge_keeps_finding_records_while_identity_holds() -> None:
    issue = _dupe()
    candidate = classify(issue, REPOSITORY).candidate
    assert candidate is not None
    link = {
        "key": candidate.key, "repository": REPOSITORY,
        "evidence_digest": candidate.evidence_digest,
        "number": 7, "url": "https://example.test/7", "state": "open",
    }
    issue["detail"]["github_repair"] = link
    existing = {issue["id"]: copy.deepcopy(issue)}
    moved = _dupe(fn_b=_function("beta", 30))
    del moved["detail"]["github_repair_revalidated"]
    upsert_issues(existing, [moved], [], "2026-10-05T00:00:00Z", lang=None)
    detail = existing[issue["id"]]["detail"]
    assert detail["github_repair"] == link
    assert "github_repair_revalidated" not in detail
