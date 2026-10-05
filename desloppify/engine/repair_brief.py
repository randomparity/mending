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
from typing import Any

from desloppify.engine.repair_manifest import SourceManifest, manifest_from_record
from desloppify.engine.repair_queue import KEY_LINE, PromotionCandidate

BRIEF_SCHEMA = "desloppify-repair-brief:v1"
MAX_TEXT = 1000
MAX_ITEMS = 20
# Below GitHub's 65536-character body limit and Linux's 131072-byte argv string limit.
MAX_BODY_BYTES = 60000
MAX_TITLE = 120
CONFIDENCE = frozenset({"high", "medium", "low"})
ASSERTIONS_HEADING = "## Reviewer assertions (not verified against source)"
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
_REJECTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "secret",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"
            r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,}"
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

    @property
    def version(self) -> str:
        """SHA-256 of the canonical brief record; a wording change moves it, never the key."""
        record = {"schema": BRIEF_SCHEMA, **asdict(self)}
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


def build_brief(
    issue: Mapping[str, Any], candidate: PromotionCandidate
) -> RepairBrief | ParkedBrief:
    """Validate and sanitize every field; any missing or unsafe value parks the brief."""
    raw_detail = issue.get("detail")
    detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
    suggestion = detail.get("suggestion")
    try:
        manifest = _manifest(detail)
        brief = RepairBrief(
            key=candidate.key,
            identity=candidate.identity,
            evidence_digest=candidate.evidence_digest,
            manifest_digest=manifest.digest,
            revision=manifest.revision,
            problem=_text("problem", issue.get("summary")),
            consequence=_text("consequence", detail.get("maintenance_consequence")),
            evidence=_items("evidence", detail.get("evidence")),
            affected=tuple((_text("affected", d.path), d.role) for d in manifest.dependencies),
            owner=_text("owner", detail.get("proposed_owner")),
            fix=None if _blank(suggestion) else _text("fix", suggestion),
            contracts=_items("contracts", detail.get("protected_contracts")),
            verification=_text("verification", detail.get("verification")),
            confidence=_confidence(issue.get("confidence")),
        )
    except _Rejected as exc:
        return ParkedBrief(exc.field, exc.reason)
    if len(render_brief(brief)[1].encode()) > MAX_BODY_BYTES:
        return ParkedBrief("body", "too-large")
    return brief


def render_brief(brief: RepairBrief) -> tuple[str, str]:
    """Return the public title and body; body source values render as inert code spans."""
    title = f"Repair: {brief.problem}"
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 3].rstrip() + "..."
    fix = ["", f"Suggested fix: {_code(brief.fix)}"] if brief.fix else []
    lines = [
        "## Source-bound",
        "",
        f"Source revision: `{brief.revision}`",
        "",
        "Allowed scope: changes to these files.",
        "",
        *(f"- {_code(path)} ({role})" for path, role in brief.affected),
        "",
        "Excluded scope: changes to any other file, and any change to a protected contract.",
        "",
        ASSERTIONS_HEADING,
        "",
        f"Problem: {_code(brief.problem)}",
        "",
        f"Maintenance consequence: {_code(brief.consequence)}",
        "",
        "Evidence:",
        "",
        *(f"- {_code(item)}" for item in brief.evidence),
        "",
        f"Proposed owner: {_code(brief.owner)}",
        *fix,
        "",
        "Protected contracts:",
        "",
        *(f"- {_code(item)}" for item in brief.contracts),
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
        "## Provenance",
        "",
        KEY_LINE.format(brief.key),
        "",
        "```text",
        f"schema: {BRIEF_SCHEMA}",
        f"concern-key: {brief.key}",
        f"concern-identity: {brief.identity}",
        f"evidence-digest: {brief.evidence_digest}",
        f"manifest-digest: {brief.manifest_digest}",
        f"source-revision: {brief.revision}",
        f"brief-version: {brief.version}",
        "```",
    ]
    return title, "\n".join(lines)


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
    "ParkedBrief",
    "RepairBrief",
    "build_brief",
    "render_brief",
]
