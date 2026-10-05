# Plan: sanitized repair brief and public-safe rendering (#20)

Goal: replace the digest-only repair issue with a versioned, sanitized brief
that parks instead of publishing unsafe or incomplete context, per
[the spec](../specs/2026-10-05-repair-brief-design.md) and
[ADR 0009](../../adr/0009-sanitized-repair-brief.md).

Architecture: a new engine module `repair_brief.py` owns the brief schema,
field validation, sanitization, rendering, and version digest. The GitHub
client takes a rendered title and body; the command module builds the brief
inside the locked create transaction and parks before any pending write.

Tech stack: Python 3.11+, stdlib `re`/`unicodedata`/`json`/`hashlib`, pytest.

Expected implementation size: 330–420 changed lines (M) — about 150
production lines (new module ~130, client and command ~25, removed renderer
~20) and about 220 test lines (new `test_brief.py`, fixture brief fields and
fake `create` signatures in three existing test files).

## Global Constraints

- No new dependencies. `repair_brief` may import `repair_queue` and
  `repair_manifest`; `repair_queue` must not import `repair_brief`.
- A parked brief prints the field and a fixed category only, never a value.
- Do not edit ADR 0005, `repair_cycle*.py`, `update_skill/`, `docs/*.md`, or
  `desloppify/data/global/`.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## File map

| File | Owns after this change |
|---|---|
| `desloppify/engine/repair_brief.py` (new) | `BRIEF_SCHEMA`, `RepairBrief`, `ParkedBrief`, `build_brief`, `render_brief` |
| `desloppify/engine/repair_queue.py` | `GitHubIssueClient.create(repository, title, body)`; `render_issue` removed |
| `desloppify/app/commands/repair_queue.py` | brief build + park in `_create_once`; dry-run park preview |
| `desloppify/tests/repair_queue/test_brief.py` (new) | Success 1, 2, 4, 5 and per-class rejection |
| `desloppify/tests/repair_queue/test_promotion.py` | drop `render_issue` test; client `create` argv test |
| `desloppify/tests/repair_queue/test_sync_matrix.py`, `desloppify/tests/commands/test_repair_queue.py` | brief fields in fixtures; fake `create(repository, title, body)`; sync park tests (Success 3) |

`render_issue` has one caller (`GitHubIssueClient.create`) and one test; both
migrate. No compatibility path is retained: the function is internal.

## Task 1 — Brief module

Files: create `desloppify/engine/repair_brief.py`, create
`desloppify/tests/repair_queue/test_brief.py`.

Interfaces (later tasks rely on these exact names):

```python
BRIEF_SCHEMA: str = "desloppify-repair-brief:v1"
@dataclass(frozen=True) class ParkedBrief: field: str; reason: str
@dataclass(frozen=True) class RepairBrief: ...; version: str (property)
def build_brief(issue: Mapping[str, Any], candidate: PromotionCandidate) -> RepairBrief | ParkedBrief
def render_brief(brief: RepairBrief) -> tuple[str, str]
```

Consumes: `PromotionCandidate`, `KEY_LINE`, `carries_concern_marker`,
`concern_key` (`desloppify.engine.repair_queue`); `SourceManifest`,
`manifest_from_record`, `MANIFEST_SCHEMA` (`desloppify.engine.repair_manifest`).

Verification:

- Contract: valid brief renders all values + provenance and keeps the key
  line. Mode: focused-test — `test_valid_brief_renders_usable_evidence`;
  red: `ModuleNotFoundError: desloppify.engine.repair_brief`; green:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_brief.py`.
- Contract: missing required field parks with `missing`. Mode: focused-test —
  `test_missing_required_field_parks` parametrized over every required
  source field, `verification` first; same red/green.
- Contract: each rejection class parks with its category and the payload
  never appears in `repr(result)`. Mode: focused-test —
  `test_unsafe_value_parks_without_leaking` parametrized over secret,
  private-identifier, link, hostile-instruction, control-character,
  too-long, plus unsafe optional `suggestion`; same red/green.
- Contract: inert rendering. Mode: focused-test —
  `test_values_render_inside_longer_fence` (value ``a `` b `` with `@x` and
  `<b>`); same red/green.
- Contract: version digest. Mode: focused-test —
  `test_version_is_stable_and_content_bound`; same red/green.
- Contract: body size cap. Mode: focused-test — `test_oversized_body_parks`
  (21 contracts → `too-long`; 20 evidence items of 990 non-ASCII chars →
  `body`/`too-large`); same red/green.

Steps:

1. Write `test_brief.py` with the six tests above. Fixture: `_issue()`
   returns a work item with `summary`, `confidence: "medium"`, and detail
   `maintenance_consequence`, `evidence` (2 items), `proposed_owner`,
   `suggestion`, `protected_contracts` (2 items), `verification`, the
   concern hashes, and `github_repair_revalidated` holding a complete
   manifest record for `src/impl.py` (implementation) and `src/sibling.py`
   (sibling). `_candidate()` is `candidate_from_issue(_issue(), REPOSITORY)`.
   Leak assertion: `payload not in repr(result)`.
2. Run the focused command; expect collection failure on the import.
3. Write the module:

```python
"""Versioned, sanitized repair brief and its public-safe rendering (ADR 0009)."""

from __future__ import annotations

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
MAX_TEXT, MAX_ITEMS, MAX_BODY_BYTES, MAX_TITLE = 1000, 20, 60000, 120
CONFIDENCE = frozenset({"high", "medium", "low"})
_REJECTIONS = (
    ("secret", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
        r"|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_\w{20,}|\bxox[abprs]-[\w-]{10,}"
        r"|\bAIza[\w-]{35}|\bsk-[\w-]{20,}|\beyJ[\w-]{10,}\.[\w-]{10,}\."
        r"|(?i:\b(?:password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*['\"][^'\"\s]{8,}['\"])"
    )),
    ("private-identifier", re.compile(
        r"[\w.+-]+@[\w-]+\.[\w.-]+|\b\d{1,3}(?:\.\d{1,3}){3}\b"
        r"|(?i:\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b)|/home/|/Users/|/root/"
        r"|(?i:\b[a-z]:\\users\\)|(?<![\w.])~/|(?i:\b(?:[a-z0-9-]+\.)+(?:internal|corp|lan|intranet)\b)"
    )),
    ("link", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://|\bwww\.|\b(?:mailto|javascript):|\bdata:\w+/")),
    ("hostile-instruction", re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\b[^.]{0,40}\b(?:previous|prior|above|earlier|all)\b"
        r"[^.]{0,20}\binstructions?\b|\bsystem prompt\b|\byou are now\b|\bnew instructions?\b"
    )),
)


class _Rejected(Exception):
    def __init__(self, field: str, reason: str) -> None:
        super().__init__(field, reason)
        self.field, self.reason = field, reason


@dataclass(frozen=True)
class ParkedBrief:
    """A brief that must not be published; carries a field and fixed category only."""

    field: str
    reason: str


@dataclass(frozen=True)
class RepairBrief:
    """Sanitized, source-bound repair brief (schema ``BRIEF_SCHEMA``)."""

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
        """SHA-256 of the canonical brief record; #22 compares it."""
        record = json.dumps({"schema": BRIEF_SCHEMA, **asdict(self)}, sort_keys=True, separators=(",", ":"))
        return sha256(record.encode()).hexdigest()


def build_brief(issue: Mapping[str, Any], candidate: PromotionCandidate) -> RepairBrief | ParkedBrief:
    """Validate and sanitize every field; any missing or unsafe value parks the brief."""
    detail = issue.get("detail") if isinstance(issue.get("detail"), Mapping) else {}
    try:
        manifest = _manifest(detail)
        suggestion = detail.get("suggestion")
        brief = RepairBrief(
            key=candidate.key, identity=candidate.identity, evidence_digest=candidate.evidence_digest,
            manifest_digest=manifest.digest, revision=manifest.revision,
            problem=_text("problem", issue.get("summary")),
            consequence=_text("consequence", detail.get("maintenance_consequence")),
            evidence=_items("evidence", detail.get("evidence")),
            affected=tuple((_text("affected", d.path), d.role) for d in manifest.dependencies),
            owner=_text("owner", detail.get("proposed_owner")),
            fix=_text("fix", suggestion) if isinstance(suggestion, str) and suggestion.strip() else None,
            contracts=_items("contracts", detail.get("protected_contracts")),
            verification=_text("verification", detail.get("verification")),
            confidence=_confidence(issue.get("confidence")),
        )
    except _Rejected as exc:
        return ParkedBrief(exc.field, exc.reason)
    if len(render_brief(brief)[1].encode()) > MAX_BODY_BYTES:
        return ParkedBrief("body", "too-large")
    return brief
```

   Followed by `render_brief` (title `Repair: <problem>` cut to 117 chars +
   `...` past 120; body sections in the spec order, each source value via
   `_code`; provenance = `KEY_LINE.format(key)`, blank line, a `text` fence
   with the seven `name: value` lines), and the helpers:

```python
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
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in text):
        raise _Rejected(field, "control-character")
    for reason, pattern in _REJECTIONS:
        if pattern.search(text):
            raise _Rejected(field, reason)
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
    if value not in CONFIDENCE:
        raise _Rejected("confidence", "invalid")
    return str(value)


def _code(text: str) -> str:
    fence = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
    return f"{fence} {text} {fence}"
```

4. Run the focused command; expect all pass. `make lint typecheck arch`.
5. Commit `feat(repair-brief): add sanitized versioned repair brief`.

## Task 2 — Publish the brief and park in sync

Files: modify `desloppify/engine/repair_queue.py`,
`desloppify/app/commands/repair_queue.py`,
`desloppify/tests/repair_queue/test_promotion.py`,
`desloppify/tests/repair_queue/test_sync_matrix.py`,
`desloppify/tests/commands/test_repair_queue.py`.

Interfaces: consumes `build_brief`, `render_brief`, `ParkedBrief` from Task
1. Produces `GitHubIssueClient.create(self, repository: str, title: str,
body: str) -> None` (argv `gh issue create --repo R --title T --body B
--label status:ready`).

Verification:

- Contract: hostile brief parks in `sync --apply` — no pending record, zero
  creates, stdout names field and category but not the payload. Mode:
  focused-test — `test_sync_parks_unsafe_brief_without_publishing` in
  `test_repair_queue.py`; red: a create call (`create_calls == 1`); green:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_queue.py`.
- Contract: dry run reports `Would park`. Mode: focused-test —
  `test_dry_run_reports_parked_brief`; same red/green.
- Contract: created body passes ADR 0008 adoption. Mode: focused-test —
  matrix fake `create` stores the given body;
  `test_base_move_without_dependency_change_creates_once` links only when
  the body carries the key line; green: `uv run --locked pytest -q desloppify/tests/repair_queue/`.
- Contract: client argv. Mode: focused-test —
  `test_client_creates_actionable_issue_with_fixed_arguments` passes
  `title, body` and asserts the fixed argv; same green.

Steps:

1. Add a module-level `BRIEF` fixture (top-level `summary`, `confidence`;
   detail brief fields) to `_state()`/`_item()` in both command and matrix
   tests; change every fake `create(self, repository, candidate)` to
   `create(self, repository, title, body)`; the matrix fake appends
   `GitHubIssue(number, url, "open", body)`. Add the two command tests.
   Replace the `render_issue` test in `test_promotion.py` with an argv test.
2. Run the command test; expect the park test to fail on a create call.
3. In `repair_queue.py`: change `create` to take `title, body`; delete
   `render_issue` and its `__all__` entry.
4. In the command: after `_recheck_locked` in `_create_once`,
   `brief = build_brief(_issues(state)[candidate.issue_id], candidate)`; on
   `ParkedBrief` print `Parked <id>: brief field <field> cannot be published
   (<reason>); hand the concern off privately.` and return before
   `github_repair_pending`; after the lock, `client.create(candidate.repository,
   *render_brief(brief))`. Replace the dry-run `Would create` print with
   `_preview_create(state, candidate)` printing `Would park …` or `Would
   create a repair issue for <id>.`
5. Run all repair-queue tests and the full guardrail list; commit
   `feat(repair-queue): publish sanitized brief and park unsafe briefs`.
