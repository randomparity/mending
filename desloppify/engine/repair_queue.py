"""Source-safe GitHub repair-queue promotion primitives."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed argv is passed to the installed gh CLI.
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from desloppify.engine.repair_manifest import SourceManifest, manifest_from_record

KEY_SCHEMA = "desloppify-concern-key:v1"
KEY_LINE = "<!-- desloppify-concern-key: {} -->"
LEGACY_LINE = "<!-- desloppify-concern: {} -->"


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


def normalize_record(
    kind: str, record: object, repository: str, identity: str, evidence_digest: str
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
    key = concern_key(repository, identity)
    if ("key" in record) == ("marker" in record):
        raise RepairRecordError(f"{kind} must carry exactly one of key or marker")
    if "key" in record:
        version = record.get("evidence_digest")
        if record["key"] != key or not _is_digest(version):
            raise RepairRecordError(f"{kind} key does not match this concern")
    elif record["marker"] == legacy_marker(identity, evidence_digest):
        version = evidence_digest
    else:
        raise RepairRecordError(f"{kind} legacy marker does not match this concern")
    normalized = {"key": key, "repository": repository, "evidence_digest": version}
    return {**normalized, **_kind_fields(kind, record)}


def _kind_fields(kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
    if kind == "github_repair_pending":
        return {}
    if kind == "github_repair":
        number, url, state = record.get("number"), record.get("url"), record.get("state")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise RepairRecordError("github_repair has an invalid issue number")
        if not isinstance(url, str) or not url or state not in {"open", "closed", None}:
            raise RepairRecordError("github_repair has an invalid url or state")
        return {"number": number, "url": url, "state": state}
    if kind == "github_repair_revalidated":
        manifest = manifest_from_record(record.get("manifest"))
        if (
            not isinstance(manifest, SourceManifest)
            or manifest.coverage != "complete"
            or record.get("manifest_digest") != manifest.digest
        ):
            raise RepairRecordError("github_repair_revalidated has no complete bound manifest")
        return {"manifest": manifest.as_record(), "manifest_digest": manifest.digest}
    raise RepairRecordError(f"unknown repair record kind {kind}")


def candidate_from_issue(
    issue: Mapping[str, Any], repository: str, *, require_revalidation: bool = True
) -> PromotionCandidate | None:
    """Return a promotion candidate only for a current revalidated concern."""
    if issue.get("detector") != "concerns" or issue.get("status") != "open":
        return None
    issue_id = issue.get("id")
    detail = issue.get("detail")
    if not isinstance(issue_id, str) or not issue_id or not isinstance(detail, Mapping):
        return None
    hashes = concern_hashes(detail)
    if hashes is None:
        return None
    if detail.get("previous_concern_identity") or detail.get("previous_concern_evidence_digest"):
        return None
    identity, evidence_digest = hashes
    candidate = PromotionCandidate(
        issue_id, identity, evidence_digest, concern_key(repository, identity), repository
    )
    if require_revalidation and not _has_current_revalidation(detail, candidate):
        return None
    return candidate


def carries_concern_marker(body: str, candidate: PromotionCandidate) -> bool:
    """Whether an issue body carries this concern's key line or current legacy line."""
    expected = {
        KEY_LINE.format(candidate.key),
        LEGACY_LINE.format(legacy_marker(candidate.identity, candidate.evidence_digest)),
    }
    return any(line.strip() in expected for line in body.splitlines())


def matching_record(
    detail: Mapping[str, Any], kind: str, candidate: PromotionCandidate
) -> dict[str, Any] | None:
    """Return the candidate's record of ``kind``; raise when it is present but unbound."""
    return normalize_record(
        kind, detail.get(kind), candidate.repository, candidate.identity, candidate.evidence_digest
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

    def create(self, repository: str, title: str, body: str) -> None:
        """Attempt one creation; callers must re-search before linking state."""
        self._call(
            [
                "gh", "issue", "create", "--repo", repository, "--title", title,
                "--body", body, "--label", "status:ready",
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
    "GitHubIssue",
    "GitHubIssueClient",
    "KEY_SCHEMA",
    "PromotionCandidate",
    "RepairRecordError",
    "candidate_from_issue",
    "carries_concern_marker",
    "concern_hashes",
    "concern_key",
    "legacy_marker",
    "matching_record",
    "normalize_record",
]
