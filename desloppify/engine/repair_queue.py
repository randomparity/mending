"""Source-safe GitHub repair-queue promotion primitives."""

from __future__ import annotations

import copy
import json
import posixpath
import subprocess  # nosec B404 - fixed argv is passed to the installed gh CLI.
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from desloppify.engine.repair_check import passing_check
from desloppify.engine.repair_manifest import (
    SourceManifest,
    concern_dependencies,
    manifest_from_record,
)

KEY_SCHEMA = "desloppify-concern-key:v1"
KEY_LINE = "<!-- desloppify-concern-key: {} -->"
LEGACY_LINE = "<!-- desloppify-concern: {} -->"
FINDING_KEY_SCHEMA = "desloppify-finding-key:v1"
FINDING_KEY_LINE = "<!-- desloppify-finding-key: {} -->"
PROPOSAL_KEY_SCHEMA = "desloppify-proposal-key:v1"
PROPOSAL_LINE = "<!-- desloppify-proposal-key: {} -->"
MAX_SMALL_FILES = 3


class RepairRecordError(ValueError):
    """A stored repair record is present but not bound to this concern."""


@dataclass(frozen=True)
class PromotionCandidate:
    """A revalidated concern eligible for one repository's repair queue."""

    issue_id: str
    identity: str
    evidence_digest: str
    key: str
    repository: str
    route: str = "concern"
    kind: str = "small_repair"


@dataclass(frozen=True)
class Classification:
    """``small_repair``, ``proposal``, or ``ineligible``; only ineligible has no candidate."""

    kind: str
    reason: str
    candidate: PromotionCandidate | None


@dataclass(frozen=True)
class Lane:
    """Where a candidate's local records live and whether its GitHub issue is dispatchable."""

    link: str
    pending: str
    ready: bool


REPAIR_LANE = Lane("github_repair", "github_repair_pending", True)
PROPOSAL_LANE = Lane("github_proposal", "github_proposal_pending", False)
LINK_KINDS = (
    "github_repair", "github_repair_pending", "github_proposal", "github_proposal_pending"
)


@dataclass(frozen=True)
class GitHubIssue:
    """The minimal GitHub issue data needed for durable local linkage."""

    number: int
    url: str
    state: str | None
    body: str = ""


Run = Callable[..., subprocess.CompletedProcess[str]]


def concern_key(repository: str, identity: str) -> str:
    """Return the stable repository-scoped key; the evidence version is never an input."""
    return sha256(f"{KEY_SCHEMA}\n{repository}\n{identity}".encode()).hexdigest()


def finding_key(repository: str, identity: str) -> str:
    """Return a mechanical finding's key; a distinct schema keeps it out of the concern space."""
    return sha256(f"{FINDING_KEY_SCHEMA}\n{repository}\n{identity}".encode()).hexdigest()


def proposal_marker(key: str) -> str:
    """A proposal's public marker: one-way, so its body never discloses the repair key."""
    return sha256(f"{PROPOSAL_KEY_SCHEMA}\n{key}".encode()).hexdigest()


def record_key(route: str, repository: str, identity: str) -> str:
    """Return the key for ``route`` (``concern`` or ``finding``)."""
    if route == "finding":
        return finding_key(repository, identity)
    return concern_key(repository, identity)


def legacy_marker(identity: str, evidence_digest: str) -> str:
    """Return the pre-stable-key identity+evidence marker, recognized only for migration."""
    return sha256(f"{identity}\n{evidence_digest}".encode()).hexdigest()


def concern_hashes(detail: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the (identity, evidence digest) pair when both are well-formed."""
    identity = detail.get("concern_identity")
    evidence_digest = detail.get("concern_evidence_digest")
    if _is_digest(identity) and _is_digest(evidence_digest):
        return identity, evidence_digest
    return None


def item_hashes(issue: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(route, identity, evidence version)`` for a routed work item, else ``None``."""
    detail = issue.get("detail")
    if not isinstance(detail, Mapping):
        return None
    if issue.get("detector") == "concerns":
        hashes = concern_hashes(detail)
        return None if hashes is None else ("concern", *hashes)
    found = _finding_hashes(issue)
    return None if found is None else ("finding", *found)


def _finding_hashes(issue: Mapping[str, Any]) -> tuple[str, str] | None:
    """Identity and evidence version of a ``dupes`` pair; ``None`` when its shape is malformed."""
    detail, file = issue.get("detail"), issue.get("file")
    if issue.get("detector") != "dupes" or not isinstance(detail, Mapping):
        return None
    functions = [detail.get("fn_a"), detail.get("fn_b")]
    if not isinstance(file, str) or file in {"", "."} or not all(map(_function, functions)):
        return None
    names = sorted(fn["name"] for fn in functions)
    anchors = sorted([fn["name"], fn["line"], fn["loc"]] for fn in functions)
    identity = {"schema": 1, "detector": "dupes", "file": file, "names": names}
    evidence = {"schema": 1, "kind": detail.get("kind"), "functions": anchors}
    return _digest(identity), _digest(evidence)


def _function(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and all(isinstance(value.get(k), str) and value[k] for k in ("file", "name"))
        and all(_positive(value.get(k)) for k in ("line", "loc"))
    )


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def normalize_record(
    kind: str,
    record: object,
    repository: str,
    identity: str,
    evidence_digest: str,
    *,
    route: str = "concern",
) -> dict[str, Any] | None:
    """Return a stored repair record in the stable-key shape.

    ``None`` means absent. A legacy record is recognized only when its marker
    matches the supplied hashes; anything else present raises
    ``RepairRecordError`` so callers fail closed.
    """
    if record is None:
        return None
    if not isinstance(record, Mapping) or record.get("repository") != repository:
        raise RepairRecordError(f"{kind} is not bound to the requested repository")
    key = record_key(route, repository, identity)
    if ("key" in record) == ("marker" in record):
        raise RepairRecordError(f"{kind} must carry exactly one of key or marker")
    if "key" in record:
        version = record.get("evidence_digest")
        if record["key"] != key or not _is_digest(version):
            raise RepairRecordError(f"{kind} key does not match this concern")
    elif route == "concern" and record["marker"] == legacy_marker(identity, evidence_digest):
        version = evidence_digest
    else:
        raise RepairRecordError(f"{kind} legacy marker does not match this concern")
    normalized = {"key": key, "repository": repository, "evidence_digest": version}
    return {**normalized, **_kind_fields(kind, record)}


def _kind_fields(kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
    if kind in {"github_repair_pending", "github_proposal_pending"}:
        return {}
    if kind in {"github_repair", "github_proposal"}:
        number, url, state = record.get("number"), record.get("url"), record.get("state")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise RepairRecordError(f"{kind} has an invalid issue number")
        if not isinstance(url, str) or not url or state not in {"open", "closed", None}:
            raise RepairRecordError(f"{kind} has an invalid url or state")
        return {"number": number, "url": url, "state": state}
    if kind == "github_repair_revalidated":
        manifest = manifest_from_record(record.get("manifest"))
        check, check_digest = record.get("check"), record.get("check_digest")
        if (
            not isinstance(manifest, SourceManifest)
            or manifest.coverage != "complete"
            or record.get("manifest_digest") != manifest.digest
            or not passing_check(check, check_digest)
        ):
            raise RepairRecordError(
                "github_repair_revalidated has no complete bound manifest and passing check"
            )
        return {
            "manifest": manifest.as_record(),
            "manifest_digest": manifest.digest,
            "check": copy.deepcopy(check),
            "check_digest": check_digest,
        }
    raise RepairRecordError(f"unknown repair record kind {kind}")


def classify(
    issue: Mapping[str, Any], repository: str, *, require_revalidation: bool = True
) -> Classification:
    """Route an open work item to a small repair, a proposal, or ineligible with a reason."""
    issue_id, detail = issue.get("id"), issue.get("detail")
    if (
        issue.get("status") != "open"
        or not isinstance(issue_id, str)
        or not issue_id
        or not isinstance(detail, Mapping)
    ):
        return _ineligible("not an open work item")
    detector = issue.get("detector")
    if detector == "concerns":
        return _concern_route(issue, issue_id, detail, repository, require_revalidation)
    if detector == "dupes":
        return _finding_route(issue, issue_id, detail, repository, require_revalidation)
    return _ineligible("detector has no repair route")


def candidate_from_issue(
    issue: Mapping[str, Any], repository: str, *, require_revalidation: bool = True
) -> PromotionCandidate | None:
    """Return a small-repair candidate only; a proposal is never a repair candidate."""
    result = classify(issue, repository, require_revalidation=require_revalidation)
    return result.candidate if result.kind == "small_repair" else None


def concern_failures(issue: Mapping[str, Any]) -> tuple[str, ...]:
    """Name each small-repair predicate a concern fails; any failure makes it a proposal."""
    detail = issue.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    paths = [spec.path for spec in concern_dependencies(issue) or ()]
    failures = []
    if len(paths) > MAX_SMALL_FILES:
        failures.append(f"scope spans {len(paths)} files; the bound is {MAX_SMALL_FILES}")
    directories = {posixpath.dirname(path) for path in paths}
    if len(directories) > 1:
        failures.append(f"files span {len(directories)} directories")
    verification = detail.get("verification")
    if not isinstance(verification, str) or not verification.strip():
        failures.append("no verification plan")
    if issue.get("confidence") != "high":
        failures.append("review confidence is not high")
    return tuple(failures)


def _concern_route(
    issue: Mapping[str, Any],
    issue_id: str,
    detail: Mapping[str, Any],
    repository: str,
    require_revalidation: bool,
) -> Classification:
    hashes = concern_hashes(detail)
    if hashes is None:
        return _ineligible("concern identity or evidence digest is malformed")
    if detail.get("previous_concern_identity") or detail.get("previous_concern_evidence_digest"):
        return _ineligible("concern changed since the last scan")
    failures = concern_failures(issue)
    kind = "proposal" if failures else "small_repair"
    candidate = PromotionCandidate(
        issue_id, *hashes, concern_key(repository, hashes[0]), repository, "concern", kind
    )
    return _gated(candidate, detail, require_revalidation, "; ".join(failures))


def _finding_route(
    issue: Mapping[str, Any],
    issue_id: str,
    detail: Mapping[str, Any],
    repository: str,
    require_revalidation: bool,
) -> Classification:
    hashes = _finding_hashes(issue)
    if hashes is None:
        return _ineligible("finding evidence is malformed")
    if (
        detail.get("kind") != "exact"
        or detail.get("cluster_size") != 2
        or detail["fn_a"]["file"] != detail["fn_b"]["file"]
    ):
        return _ineligible("finding exceeds the small-repair bound")
    candidate = PromotionCandidate(
        issue_id, *hashes, finding_key(repository, hashes[0]), repository, "finding"
    )
    return _gated(candidate, detail, require_revalidation, "")


def _gated(
    candidate: PromotionCandidate,
    detail: Mapping[str, Any],
    require_revalidation: bool,
    reason: str,
) -> Classification:
    if require_revalidation and not _has_current_revalidation(detail, candidate):
        return _ineligible("no current source-bound revalidation")
    return Classification(candidate.kind, reason or "within the small-repair bound", candidate)


def _ineligible(reason: str) -> Classification:
    return Classification("ineligible", reason, None)


def lane_for(candidate: PromotionCandidate) -> Lane:
    """A proposal's records and issue live outside the repair-dispatch lane."""
    return PROPOSAL_LANE if candidate.kind == "proposal" else REPAIR_LANE


def marker_line(candidate: PromotionCandidate) -> str:
    """The body line that identifies this candidate's lane and key."""
    if candidate.kind == "proposal":
        return PROPOSAL_LINE.format(proposal_marker(candidate.key))
    line = FINDING_KEY_LINE if candidate.route == "finding" else KEY_LINE
    return line.format(candidate.key)


def carries_concern_marker(body: str, candidate: PromotionCandidate) -> bool:
    """Whether an issue body carries this candidate's own lane line (or current legacy line)."""
    expected = {marker_line(candidate)}
    if candidate.route == "concern" and candidate.kind == "small_repair":
        expected.add(
            LEGACY_LINE.format(legacy_marker(candidate.identity, candidate.evidence_digest))
        )
    return any(line.strip() in expected for line in body.splitlines())


def matching_record(
    detail: Mapping[str, Any], kind: str, candidate: PromotionCandidate
) -> dict[str, Any] | None:
    """Return the candidate's record of ``kind``; raise when it is present but unbound."""
    return normalize_record(
        kind,
        detail.get(kind),
        candidate.repository,
        candidate.identity,
        candidate.evidence_digest,
        route=candidate.route,
    )


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _has_current_revalidation(detail: Mapping[str, Any], candidate: PromotionCandidate) -> bool:
    try:
        record = matching_record(detail, "github_repair_revalidated", candidate)
    except RepairRecordError:
        return False
    return record is not None and record["evidence_digest"] == candidate.evidence_digest


class GitHubIssueClient:
    """Minimal injected wrapper around fixed-argument ``gh`` operations."""

    def __init__(self, run: Run = subprocess.run) -> None:
        self._run = run

    def resolve_repository(self, repository: str) -> str:
        """Verify that ``gh`` resolves exactly the explicitly requested repository."""
        payload = self._json(
            ["gh", "repo", "view", repository, "--json", "nameWithOwner"]
        )
        if not isinstance(payload, Mapping) or payload.get("nameWithOwner") != repository:
            raise ValueError("repository resolution did not match the requested repository")
        return repository

    def search(self, repository: str, marker: str) -> list[GitHubIssue]:
        """Find marker matches across open and closed issues."""
        payload = self._json(
            [
                "gh", "issue", "list", "--repo", repository, "--state", "all",
                "--search", marker, "--limit", "100", "--json", "number,url,state,body",
            ]
        )
        return _decode_issues(payload, allow_state=True)

    def view(self, repository: str, number: int) -> GitHubIssue:
        """Read a linked issue independent of mutable body-marker text."""
        payload = self._json(
            ["gh", "issue", "view", str(number), "--repo", repository, "--json", "number,url,state"]
        )
        issues = _decode_issues([payload], allow_state=True)
        return issues[0]

    def create(self, repository: str, title: str, body: str, *, ready: bool) -> None:
        """Attempt one creation; only a dispatchable (``ready``) issue gets ``status:ready``."""
        labels = ["--label", "status:ready"] if ready else []
        self._call(
            [
                "gh", "issue", "create", "--repo", repository, "--title", title,
                "--body", body, *labels,
            ]
        )

    def _json(self, argv: Sequence[str]) -> object:
        process = self._call(argv)
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("gh returned malformed JSON") from exc

    def _call(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        process = self._run(list(argv), capture_output=True, text=True, check=False)
        if process.returncode != 0:
            raise RuntimeError("gh command failed")
        return process


def _decode_issues(payload: object, *, allow_state: bool) -> list[GitHubIssue]:
    if not isinstance(payload, list):
        raise ValueError("gh returned an invalid issue list")
    issues: list[GitHubIssue] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("gh returned an invalid issue item")
        number = item.get("number")
        url = item.get("url")
        state = item.get("state")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1 or not isinstance(url, str) or not url:
            raise ValueError("gh returned an invalid issue item")
        if state is not None and (not allow_state or state not in {"OPEN", "CLOSED"}):
            raise ValueError("gh returned an invalid issue state")
        body = item.get("body", "")
        if not isinstance(body, str):
            raise ValueError("gh returned an invalid issue body")
        normalized_state = state.lower() if isinstance(state, str) else None
        issues.append(GitHubIssue(number, url, normalized_state, body))
    return issues


__all__ = [
    "Classification",
    "FINDING_KEY_LINE",
    "FINDING_KEY_SCHEMA",
    "GitHubIssue",
    "GitHubIssueClient",
    "KEY_SCHEMA",
    "LINK_KINDS",
    "Lane",
    "PROPOSAL_LANE",
    "PROPOSAL_KEY_SCHEMA",
    "PROPOSAL_LINE",
    "PromotionCandidate",
    "REPAIR_LANE",
    "RepairRecordError",
    "candidate_from_issue",
    "carries_concern_marker",
    "classify",
    "concern_failures",
    "concern_hashes",
    "concern_key",
    "finding_key",
    "item_hashes",
    "lane_for",
    "legacy_marker",
    "marker_line",
    "matching_record",
    "normalize_record",
    "proposal_marker",
    "record_key",
]
