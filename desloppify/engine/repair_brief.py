"""Versioned, sanitized repair brief and its public-safe rendering (ADR 0009).

Every value comes from imported review output and is untrusted. A value that is
missing, wrong-typed, or matches a rejected shape parks the whole brief; nothing
is published and the park names only the field and a fixed category.
"""

from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any, ClassVar

from desloppify.engine.repair_manifest import SourceManifest, manifest_from_record
from desloppify.engine.repair_queue import (
    FINDING_KEY_LINE,
    KEY_LINE,
    PROPOSAL_LINE,
    PromotionCandidate,
    concern_failures,
    proposal_marker,
)

BRIEF_SCHEMA = "desloppify-repair-brief:v1"
PROPOSAL_SCHEMA = "desloppify-proposal-brief:v1"
MAX_TEXT = 1000
MAX_ITEMS = 20
# Below GitHub's 65536-character body limit and Linux's 131072-byte argv string limit.
MAX_BODY_BYTES = 60000
MAX_TITLE = 120
CONFIDENCE = frozenset({"high", "medium", "low"})
ASSERTIONS_HEADING = "## Reviewer assertions (not verified against source)"
FINDING_HEADING = "## Detector finding (anchors checked against source; guidance is fixed text)"
_FINDING_GUIDANCE = {
    "consequence": "Two identical function bodies in one file must be changed together;"
    " a fix applied to one silently misses the other.",
    "fix": "Extract one shared implementation that both functions call, keeping each"
    " function's signature and callers working.",
    "contracts": ["Every existing caller of either function keeps its current behavior."],
    "verification": "The project's existing tests pass, and a fresh desloppify scan no longer"
    " reports this duplicate pair.",
}
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
_REJECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "secret",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"
            r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}|xox[abceprs]-[\w-]{10,}"
            r"|AIza[\w-]{35}|sk-[\w-]{32,}|eyJ[\w-]{10,}\.[\w-]{10,}\."
            r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*['\"][^'\"\s]{8,}['\"])"
        ),
    ),
    (
        "private-identifier",
        re.compile(
            r"[\w.+-]+@[\w-]+\.[\w.-]+|(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"
            r"|/home/|/Users/|/root/"
            r"|(?i:\b[a-z]:\\users\\)|(?<![\w.])~/"
            r"|(?i:\b(?:[a-z0-9-]+\.)+(?:internal|corp|lan|intranet)\b)"
        ),
    ),
    (
        "link",
        re.compile(
            r"(?i)\b[a-z][a-z0-9+.-]*://|(?<![:\w])//[\w-]+\.[\w.-]+|\bwww\."
            r"|\b(?:mailto|javascript):|\bdata:\w+/"
            r"|\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|dev|app|co|me|info|xyz|example)/"
        ),
    ),
    (
        "hostile-instruction",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\b[^.]{0,40}\b(?:previous|prior|above|earlier|all)\b"
            r"[^.]{0,20}\binstructions?\b|\bsystem prompt\b|\byou are now\b|\bnew instructions?\b"
        ),
    ),
)
# The problem becomes the issue title, which GitHub renders outside a code span.
_REFERENCE = re.compile(
    r"(?<!\w)#\d+|\b[\w.-]+/[\w.-]+#\d+|(?i:\bGH-\d+)|(?<!\w)@[A-Za-z0-9][\w-]*"
)


class _Rejected(Exception):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(field, reason)
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class ParkedBrief:
    """A brief that must not be published; carries a field and a fixed category only."""

    field: str
    reason: str


@dataclass(frozen=True)
class RepairBrief:
    """A sanitized, source-bound repair brief (schema ``BRIEF_SCHEMA``)."""

    schema: ClassVar[str] = BRIEF_SCHEMA

    key: str
    identity: str
    evidence_digest: str
    manifest_digest: str
    revision: str
    problem: str
    consequence: str
    evidence: tuple[str, ...]
    affected: tuple[tuple[str, str], ...]
    owner: str
    fix: str | None
    contracts: tuple[str, ...]
    verification: str
    confidence: str
    route: str = "concern"

    @property
    def version(self) -> str:
        """SHA-256 of the canonical brief record; a wording change moves it, never the key."""
        record = {"schema": self.schema, **asdict(self)}
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ProposalBrief(RepairBrief):
    """A non-dispatchable architecture proposal (schema ``PROPOSAL_SCHEMA``)."""

    schema: ClassVar[str] = PROPOSAL_SCHEMA

    questions: tuple[str, ...] = ()


def build_brief(
    issue: Mapping[str, Any], candidate: PromotionCandidate
) -> RepairBrief | ParkedBrief:
    """Validate and sanitize every field; any missing or unsafe value parks the brief."""
    raw_detail = issue.get("detail")
    detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
    source = (
        _finding_source(issue, detail) if candidate.route == "finding" else _concern_source(detail)
    )
    suggestion = source["fix"]
    try:
        manifest = _manifest(detail)
        fields = {
            "key": candidate.key,
            "identity": candidate.identity,
            "evidence_digest": candidate.evidence_digest,
            "manifest_digest": manifest.digest,
            "revision": manifest.revision,
            "problem": _text("problem", issue.get("summary")),
            "consequence": _text("consequence", source["consequence"]),
            "evidence": _items("evidence", source["evidence"]),
            "affected": tuple((_text("affected", d.path), d.role) for d in manifest.dependencies),
            "owner": _text("owner", source["owner"]),
            "fix": None if _blank(suggestion) else _text("fix", suggestion),
            "contracts": _items("contracts", source["contracts"]),
            "verification": _text("verification", source["verification"]),
            "confidence": _confidence(issue.get("confidence")),
            "route": candidate.route,
        }
        brief: RepairBrief = (
            ProposalBrief(**fields, questions=_items("questions", list(concern_failures(issue))))
            if candidate.kind == "proposal"
            else RepairBrief(**fields)
        )
    except _Rejected as exc:
        return ParkedBrief(exc.field, exc.reason)
    if len(render_brief(brief)[1].encode()) > MAX_BODY_BYTES:
        return ParkedBrief("body", "too-large")
    return brief


def _concern_source(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "consequence": detail.get("maintenance_consequence"),
        "evidence": detail.get("evidence"),
        "owner": detail.get("proposed_owner"),
        "fix": detail.get("suggestion"),
        "contracts": detail.get("protected_contracts"),
        "verification": detail.get("verification"),
    }


def _finding_source(issue: Mapping[str, Any], detail: Mapping[str, Any]) -> dict[str, Any]:
    """Fixed guidance plus the anchors; the item's relative file, never ``fn_*.file``."""
    path = issue.get("file")
    evidence = []
    for key in ("fn_a", "fn_b"):
        function = detail.get(key)
        if isinstance(function, Mapping):
            name, line, loc = function.get("name"), function.get("line"), function.get("loc")
            evidence.append(f"{path}:{line} `{name}` ({loc} lines)")
    return {**_FINDING_GUIDANCE, "evidence": evidence, "owner": path}


def render_brief(brief: RepairBrief) -> tuple[str, str]:
    """Return the public title and body; body source values render as inert code spans."""
    if isinstance(brief, ProposalBrief):
        return _title("Proposal: ", brief.problem), "\n".join(_proposal_lines(brief))
    fix = ["", f"Suggested fix: {_code(brief.fix)}"] if brief.fix else []
    lines = [
        "## Source-bound",
        "",
        f"Source revision: `{brief.revision}`",
        "",
        "Allowed scope: changes to these files.",
        "",
        *_affected(brief),
        "",
        "Excluded scope: changes to any other file, and any change to a protected contract.",
        "",
        FINDING_HEADING if brief.route == "finding" else ASSERTIONS_HEADING,
        "",
        *_observed(brief),
        "",
        f"Proposed owner: {_code(brief.owner)}",
        *fix,
        "",
        *_contracts(brief),
        "",
        f"Required verification: {_code(brief.verification)}",
        "",
        "## Risk and uncertainty",
        "",
        f"Review confidence: {brief.confidence}. Only the source-bound section was checked"
        " against source; re-check the reviewer assertions before acting.",
        "",
        "## Completion criterion",
        "",
        "The proposed change is in place within the allowed scope, the required verification"
        " passes, and every protected contract still holds.",
        "",
        *_provenance(brief),
    ]
    return _title("Repair: ", brief.problem), "\n".join(lines)


def _proposal_lines(brief: ProposalBrief) -> list[str]:
    suggestion = (
        [
            f"- Apply the reviewer's suggestion: {_code(brief.fix)}. Trade-off: one change"
            " across the affected files, which exceeds the small-repair bound.",
        ]
        if brief.fix
        else []
    )
    return [
        "## Architecture proposal: human decision required",
        "",
        "This proposal is not ready for repair and must not be dispatched. Approving it"
        " leads only to a separately scoped, revalidated execution decision.",
        "",
        f"Source revision: `{brief.revision}`",
        "",
        "Affected files (observed):",
        "",
        *_affected(brief),
        "",
        ASSERTIONS_HEADING,
        "",
        *_observed(brief),
        "",
        "## Alternatives and trade-offs",
        "",
        *suggestion,
        "- Split the work into separately scoped small repairs, each in one directory and"
        " at most three files. Trade-off: more issues to track; each is independently"
        " verifiable.",
        "- Keep the current structure and dismiss the concern. Trade-off: no change risk;"
        " the maintenance consequence remains.",
        "",
        "## Expected ownership and contracts",
        "",
        f"Proposed owner: {_code(brief.owner)}",
        "",
        *_contracts(brief),
        "",
        f"Required verification for any approved change: {_code(brief.verification)}",
        "",
        "## Open questions",
        "",
        *(f"- {_code(question)}" for question in brief.questions),
        "",
        "## Decision needed",
        "",
        f"Review confidence: {brief.confidence}. Choose one alternative or reject this"
        " proposal, and record the decision on this issue. A chosen change must be scoped"
        " and revalidated as its own repair before any execution.",
        "",
        *_provenance(brief),
    ]


def _title(prefix: str, problem: str) -> str:
    title = f"{prefix}{problem}"
    return title if len(title) <= MAX_TITLE else title[: MAX_TITLE - 3].rstrip() + "..."


def _affected(brief: RepairBrief) -> list[str]:
    return [f"- {_code(path)} ({role})" for path, role in brief.affected]


def _observed(brief: RepairBrief) -> list[str]:
    return [
        f"Problem: {_code(brief.problem)}",
        "",
        f"Maintenance consequence: {_code(brief.consequence)}",
        "",
        "Evidence:",
        "",
        *(f"- {_code(item)}" for item in brief.evidence),
    ]


def _contracts(brief: RepairBrief) -> list[str]:
    return ["Protected contracts:", "", *(f"- {_code(item)}" for item in brief.contracts)]


def _provenance(brief: RepairBrief) -> list[str]:
    if isinstance(brief, ProposalBrief):
        marker = proposal_marker(brief.key)
        line, keys = PROPOSAL_LINE.format(marker), [f"proposal-key: {marker}"]
    else:
        line = (FINDING_KEY_LINE if brief.route == "finding" else KEY_LINE).format(brief.key)
        keys = [f"{brief.route}-key: {brief.key}", f"{brief.route}-identity: {brief.identity}"]
    return [
        "## Provenance",
        "",
        line,
        "",
        "```text",
        f"schema: {brief.schema}",
        *keys,
        f"evidence-digest: {brief.evidence_digest}",
        f"manifest-digest: {brief.manifest_digest}",
        f"source-revision: {brief.revision}",
        f"brief-version: {brief.version}",
        "```",
    ]


def _manifest(detail: Mapping[str, Any]) -> SourceManifest:
    record = detail.get("github_repair_revalidated")
    manifest = manifest_from_record(record.get("manifest") if isinstance(record, Mapping) else None)
    if not isinstance(manifest, SourceManifest) or manifest.coverage != "complete":
        raise _Rejected("revision", "unbound")
    return manifest


def _text(field: str, value: object) -> str:
    if value is None:
        raise _Rejected(field, "missing")
    if not isinstance(value, str):
        raise _Rejected(field, "invalid")
    text = " ".join(value.split())
    if not text:
        raise _Rejected(field, "missing")
    if len(text) > MAX_TEXT:
        raise _Rejected(field, "too-long")
    if any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in text):
        raise _Rejected(field, "control-character")
    for reason, pattern in _REJECTIONS:
        if pattern.search(text):
            raise _Rejected(field, reason)
    if _has_ipv6(text):
        raise _Rejected(field, "private-identifier")
    if field == "problem" and _REFERENCE.search(text):
        raise _Rejected(field, "reference")
    return text


def _items(field: str, value: object) -> tuple[str, ...]:
    if not value:
        raise _Rejected(field, "missing")
    if not isinstance(value, list):
        raise _Rejected(field, "invalid")
    if len(value) > MAX_ITEMS:
        raise _Rejected(field, "too-long")
    return tuple(_text(field, item) for item in value)


def _confidence(value: object) -> str:
    if value is None:
        raise _Rejected("confidence", "missing")
    if not isinstance(value, str) or value not in CONFIDENCE:
        raise _Rejected("confidence", "invalid")
    return value


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _has_ipv6(text: str) -> bool:
    for token in re.findall(r"[0-9A-Fa-f:]{2,}", text):
        if ("::" in token or token.count(":") >= 3) and any(c.isdigit() for c in token):
            try:
                ipaddress.IPv6Address(token)
            except ValueError:
                continue
            return True
    return False


def _code(text: str) -> str:
    """Wrap ``text`` in a code span whose fence outruns every backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


__all__ = [
    "ASSERTIONS_HEADING",
    "BRIEF_SCHEMA",
    "FINDING_HEADING",
    "PROPOSAL_SCHEMA",
    "ParkedBrief",
    "ProposalBrief",
    "RepairBrief",
    "build_brief",
    "render_brief",
]
