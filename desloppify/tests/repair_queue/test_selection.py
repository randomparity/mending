"""One-per-run repair selection, ranking, and the reviewed brief version (ADR 0014)."""

from __future__ import annotations

import copy

from desloppify.engine.repair_brief import reviewed_version
from desloppify.engine.repair_check import CheckResult, Citation, Claim
from desloppify.engine.repair_manifest import MANIFEST_SCHEMA, manifest_from_record
from desloppify.engine.repair_queue import (
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
    reworded["detail"]["maintenance_consequence"] = "The two policies drift apart"
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
