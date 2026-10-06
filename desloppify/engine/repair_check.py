"""Bounded evidence-anchor check over a concern's recorded source.

The check proves only that the reviewer's ``PATH:LINE`` citations and quoted
identifiers still hold in the recorded blobs; it never proves that a concern is
valid. Anything it cannot establish is ``unknown``, never ``pass``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeGuard, TypeVar

from desloppify.engine.repair_manifest import (
    MAX_DEPENDENCIES,
    SourceManifest,
    blob_size,
    read_blob,
)

CHECK_SCHEMA = "desloppify-repair-check:v1"
MAX_EVIDENCE_ITEMS = 32
MAX_EVIDENCE_BYTES = 16_384
MAX_CITATIONS = MAX_DEPENDENCIES
MAX_FILE_BYTES = 1 << 20
MAX_TOTAL_BYTES = 8 << 20
MAX_SECONDS = 10

_CITATION = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]*\w\.\w+):(\d{1,7})(?:-(\d{1,7}))?(?!\d)"
)
_QUOTE = re.compile(r"`([^`\n]{1,200})`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_WORD = re.compile(r"\w+")
_DIGESTED = ("schema", "outcome", "claims")
_T = TypeVar("_T")


@dataclass(frozen=True)
class Citation:
    """A cited line range in one recorded dependency."""

    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Claim:
    """One evidence item's resolved citations and the identifiers it quotes."""

    citations: tuple[Citation, ...]
    identifiers: tuple[str, ...]


@dataclass(frozen=True)
class _CitedBlob:
    """What the check needs from one cited blob: its line count, words, and text."""

    lines: int
    words: frozenset[str]
    text: str


@dataclass(frozen=True)
class CheckResult:
    """The check outcome; ``transient`` marks an unknown caused by reading, not input."""

    outcome: str
    reason: str
    claims: tuple[Claim, ...]
    transient: bool = False

    def as_record(self) -> dict[str, Any]:
        """Return the persisted form: checks run, result, and bounds applied."""
        return {
            "schema": CHECK_SCHEMA,
            "outcome": self.outcome,
            "reason": self.reason,
            "transient": self.transient,
            "claims": [
                {
                    "citations": [
                        {"path": c.path, "start": c.start, "end": c.end} for c in claim.citations
                    ],
                    "identifiers": list(claim.identifiers),
                }
                for claim in self.claims
            ],
            "bounds": _bounds(),
        }

    @property
    def digest(self) -> str:
        """SHA-256 of what was checked and its outcome; reasons and bounds are excluded."""
        return _digest(self.as_record())


def check_concern(root: Path, manifest: SourceManifest, issue: Mapping[str, Any]) -> CheckResult:
    """Check ``issue``'s evidence anchors against the blobs ``manifest`` recorded under ``root``."""
    return _run_claims(root, manifest, _claims(issue, manifest))


def check_finding(root: Path, manifest: SourceManifest, issue: Mapping[str, Any]) -> CheckResult:
    """Check a ``dupes`` finding's anchors and that its two spans are still duplicates."""
    return _run_claims(root, manifest, _finding_claims(issue, manifest), _span_failure)


def _run_claims(
    root: Path,
    manifest: SourceManifest,
    claims: tuple[Claim, ...] | str,
    extra: Callable[[tuple[Claim, ...], Mapping[str, _CitedBlob]], str | None] | None = None,
) -> CheckResult:
    if isinstance(claims, str):
        return CheckResult("unknown", claims, ())
    sources = _read_cited(root, manifest, claims)
    if isinstance(sources, CheckResult):
        return sources
    for claim in claims:
        failure = _claim_failure(claim, sources)
        if failure:
            return CheckResult("fail", failure, claims)
    failure = extra(claims, sources) if extra else None
    if failure:
        return CheckResult("fail", failure, claims)
    return CheckResult("pass", "evidence anchors hold", claims)


def _span_failure(claims: tuple[Claim, ...], sources: Mapping[str, _CitedBlob]) -> str | None:
    """Each name sits on its span's first line; the spans' remaining lines are equal."""
    bodies = []
    for claim in claims:
        citation = claim.citations[0]
        lines = sources[citation.path].text.splitlines()[citation.start - 1 : citation.end]
        words = set(_WORD.findall(lines[0])) if lines else set()
        if any(name not in words for name in claim.identifiers):
            return "function name is not on its first line"
        bodies.append([line.strip() for line in lines[1:]])
    return None if bodies[0] == bodies[1] else "duplicate spans differ"


def passing_check(record: object, digest: object) -> bool:
    """Whether ``record`` is a v1 passing check whose digest is ``digest``."""
    if not isinstance(record, Mapping):
        return False
    try:
        return (
            record["schema"] == CHECK_SCHEMA
            and record["outcome"] == "pass"
            and _digest(record) == digest
        )
    except (KeyError, TypeError, ValueError):
        return False


def _bounds() -> dict[str, int]:
    return {
        "evidence_items": MAX_EVIDENCE_ITEMS,
        "evidence_bytes": MAX_EVIDENCE_BYTES,
        "citations": MAX_CITATIONS,
        "file_bytes": MAX_FILE_BYTES,
        "total_bytes": MAX_TOTAL_BYTES,
        "seconds": MAX_SECONDS,
    }


def _digest(record: Mapping[str, Any]) -> str:
    digested = {key: record[key] for key in _DIGESTED}
    canonical = json.dumps(digested, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


def _claims(issue: Mapping[str, Any], manifest: SourceManifest) -> tuple[Claim, ...] | str:
    detail = issue.get("detail")
    evidence = detail.get("evidence") if isinstance(detail, Mapping) else None
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        return "evidence is not a list of strings"
    size = sum(len(item.encode("utf-8", "surrogatepass")) for item in evidence)
    if len(evidence) > MAX_EVIDENCE_ITEMS or size > MAX_EVIDENCE_BYTES:
        return "evidence exceeds its bound"
    present = {d.path for d in manifest.dependencies if d.status == "present"}
    claims = [claim for claim in (_claim(item, present) for item in evidence) if claim]
    cited = sum(len(claim.citations) for claim in claims)
    if cited == 0:
        return "evidence cites no recorded source line"
    if cited > MAX_CITATIONS:
        return "citations exceed their bound"
    return tuple(claims)


def _finding_claims(
    issue: Mapping[str, Any], manifest: SourceManifest
) -> tuple[Claim, ...] | str:
    """One claim per duplicate function: its line range in the item file and its bare name."""
    path, detail = issue.get("file"), issue.get("detail")
    present = {d.path for d in manifest.dependencies if d.status == "present"}
    if path not in present or not isinstance(detail, Mapping):
        return "finding file is not a recorded source file"
    claims = []
    for key in ("fn_a", "fn_b"):
        function = detail.get(key)
        if not isinstance(function, Mapping):
            return "finding evidence is malformed"
        name, line, loc = function.get("name"), function.get("line"), function.get("loc")
        if not (isinstance(name, str) and _positive(line) and _positive(loc)):
            return "finding evidence is malformed"
        identifiers = tuple(_IDENTIFIER.findall(name.rsplit(".", 1)[-1]))[:1]
        claims.append(Claim((Citation(str(path), line, line + loc - 1),), identifiers))
    return tuple(claims)


def _positive(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10**7


def _claim(item: str, present: set[str]) -> Claim | None:
    citations = []
    for match in _CITATION.finditer(item):
        path = _resolve(match.group(1), present)
        if path is not None:
            start = int(match.group(2))
            citations.append(Citation(path, start, int(match.group(3) or start)))
    if not citations:
        return None
    identifiers: list[str] = []
    for span in _QUOTE.findall(item):
        if _CITATION.search(span) or "/" in span or _resolve(span.strip(), present):
            continue
        identifiers.extend(name for name in _IDENTIFIER.findall(span) if name not in identifiers)
    return Claim(tuple(citations), tuple(identifiers))


def _resolve(token: str, present: set[str]) -> str | None:
    if token in present:
        return token
    matches = [path for path in present if path.endswith("/" + token)]
    return matches[0] if len(matches) == 1 else None


def _read_cited(
    root: Path, manifest: SourceManifest, claims: tuple[Claim, ...]
) -> dict[str, _CitedBlob] | CheckResult:
    deadline = time.monotonic() + MAX_SECONDS
    objects = {d.path: d.object_id for d in manifest.dependencies}
    sources: dict[str, _CitedBlob] = {}
    total = 0
    for path in sorted({c.path for claim in claims for c in claim.citations}):
        object_id = objects[path] or ""
        size = _bounded(root, object_id, deadline, blob_size)
        if isinstance(size, CheckResult):
            return size
        total += size
        if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            return CheckResult("unknown", "source exceeds the byte bound", claims)
        text = _bounded(root, object_id, deadline, read_blob)
        if isinstance(text, CheckResult):
            return text
        sources[path] = _CitedBlob(_line_count(text), frozenset(_WORD.findall(text)), text)
    if time.monotonic() > deadline:
        return CheckResult("unknown", "time bound exceeded", (), transient=True)
    return sources


def _bounded(
    root: Path, object_id: str, deadline: float, read: Callable[[Path, str, float], _T | None]
) -> _T | CheckResult:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return CheckResult("unknown", "time bound exceeded", (), transient=True)
    value = read(root, object_id, remaining)
    if value is None:
        return CheckResult("unknown", "source file could not be read", (), transient=True)
    return value


def _claim_failure(claim: Claim, sources: Mapping[str, _CitedBlob]) -> str | None:
    for citation in claim.citations:
        if not 1 <= citation.start <= citation.end <= sources[citation.path].lines:
            return "cited line is outside the file"
    cited = [sources[c.path].words for c in claim.citations]
    if any(all(name not in words for words in cited) for name in claim.identifiers):
        return "quoted identifier is absent from the cited files"
    return None


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


__all__ = [
    "CHECK_SCHEMA",
    "CheckResult",
    "Citation",
    "Claim",
    "MAX_CITATIONS",
    "MAX_EVIDENCE_BYTES",
    "MAX_EVIDENCE_ITEMS",
    "MAX_FILE_BYTES",
    "MAX_SECONDS",
    "MAX_TOTAL_BYTES",
    "check_concern",
    "check_finding",
    "passing_check",
]
