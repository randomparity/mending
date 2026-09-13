"""Source-safe GitHub repair-queue promotion primitives."""

from __future__ import annotations

import json
import subprocess  # nosec B404 - fixed argv is passed to the installed gh CLI.
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class PromotionCandidate:
    """A revalidated concern eligible for one repository's repair queue."""

    issue_id: str
    identity: str
    evidence_digest: str
    marker: str
    repository: str


@dataclass(frozen=True)
class GitHubIssue:
    """The minimal GitHub issue data needed for durable local linkage."""

    number: int
    url: str
    state: str | None


Run = Callable[..., subprocess.CompletedProcess[str]]


def marker_for_hashes(identity: str, evidence_digest: str) -> str:
    """Return the deterministic opaque marker for a concern hash pair."""
    return sha256(f"{identity}\n{evidence_digest}".encode()).hexdigest()


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
    identity = detail.get("concern_identity")
    evidence_digest = detail.get("concern_evidence_digest")
    if not _is_digest(identity) or not _is_digest(evidence_digest):
        return None
    if detail.get("previous_concern_identity") or detail.get("previous_concern_evidence_digest"):
        return None
    marker = marker_for_hashes(identity, evidence_digest)
    if require_revalidation and not _has_current_revalidation(
        detail.get("github_repair_revalidated"), marker, repository
    ):
        return None
    return PromotionCandidate(issue_id, identity, evidence_digest, marker, repository)


def render_issue(candidate: PromotionCandidate) -> tuple[str, str]:
    """Render a public body containing structural provenance, never source text."""
    title = f"Validated repair {candidate.marker[:12]}"
    body = "\n".join(
        (
            "## Repair queue record",
            "",
            f"<!-- desloppify-concern: {candidate.marker} -->",
            f"Concern identity digest: `{candidate.identity}`",
            f"Evidence digest: `{candidate.evidence_digest}`",
            "Ownership: retained in the local validated concern record.",
            "Protected contracts: retained in the local validated concern record.",
            "Verification: retained in the local validated concern record.",
        )
    )
    return title, body


def matching_record(
    detail: Mapping[str, Any], key: str, candidate: PromotionCandidate
) -> Mapping[str, Any] | None:
    """Return metadata only when it remains bound to this marker and repository."""
    record = detail.get(key)
    if _has_matching_record(record, candidate.marker, candidate.repository):
        return record
    return None


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _has_matching_record(record: object, marker: str, repository: str) -> bool:
    return isinstance(record, Mapping) and record.get("marker") == marker and record.get("repository") == repository


def _has_current_revalidation(record: object, marker: str, repository: str) -> bool:
    attestation = record.get("attestation") if isinstance(record, Mapping) else None
    return (
        _has_matching_record(record, marker, repository)
        and isinstance(attestation, str)
        and bool(attestation.strip())
    )


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
                "--search", marker, "--limit", "100", "--json", "number,url,state",
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

    def create(self, repository: str, candidate: PromotionCandidate) -> None:
        """Attempt one creation; callers must re-search before linking state."""
        title, body = render_issue(candidate)
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
        issues.append(GitHubIssue(number, url, state.lower() if isinstance(state, str) else None))
    return issues


__all__ = [
    "GitHubIssue",
    "GitHubIssueClient",
    "PromotionCandidate",
    "candidate_from_issue",
    "marker_for_hashes",
    "matching_record",
    "render_issue",
]
