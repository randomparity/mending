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

Expected implementation size: 420–490 changed lines (M) — about 210
production lines (new module ~185 including `render_brief`, client and
command ~25, removed renderer ~20) and about 240 test lines (new
`test_brief.py`, fixture brief fields, seven fake `create` signatures in
two test files, the `render_issue` test in `test_promotion.py`).

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
  too-long, compressed IPv6 (`2001:db8::7334`), lone surrogate
  (`"\ud800"`), plus unsafe and non-string optional `suggestion` and list
  `confidence` (→ `invalid`); same red/green.
- Contract: unsupported-claim labelling. Mode: focused-test —
  `test_prose_renders_as_reviewer_assertion` (problem/evidence/fix appear
  only after the reviewer-assertions heading; `detail.related_files` naming
  `other.py` does not put `other.py` in the body); same red/green.
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
3. Write the module. Contents, in order:
   - constants `BRIEF_SCHEMA`, `MAX_TEXT = 1000`, `MAX_ITEMS = 20`,
     `MAX_BODY_BYTES = 60000`, `MAX_TITLE = 120`, `CONFIDENCE`;
   - `_REJECTIONS: tuple[tuple[str, re.Pattern[str]], ...]` — one compiled
     pattern per category (`secret`, `private-identifier`, `link`,
     `hostile-instruction`) holding exactly the shapes the spec's
     *Validation and sanitization* paragraph lists, plus
     `_has_ipv6(text) -> bool` (`re.findall(r"[0-9A-Fa-f:]{2,}", text)`
     tokens holding `::` or three colons and a digit that
     `ipaddress.ip_address` accepts);
   - `class _Rejected(Exception)` with `field`, `reason`;
   - `ParkedBrief`, `RepairBrief` (fields `key, identity, evidence_digest,
     manifest_digest, revision, problem, consequence, evidence: tuple[str,
     ...], affected: tuple[tuple[str, str], ...], owner, fix: str | None,
     contracts: tuple[str, ...], verification, confidence`; `version` =
     `sha256(json.dumps({"schema": BRIEF_SCHEMA, **asdict(self)},
     sort_keys=True, separators=(",", ":")).encode()).hexdigest()`);
   - `build_brief`: read `detail` (non-mapping → `{}`); inside `try`, bind
     the manifest (`_manifest`: `manifest_from_record` of
     `github_repair_revalidated.manifest`, must be a complete
     `SourceManifest`, else `_Rejected("revision", "unbound")`), then build
     each field in table order with `_text(field, value)` / `_items(field,
     value)`; `fix` is `None` when the suggestion is `None` or a blank
     string, else `_text("fix", suggestion)`; `confidence` must be a `str`
     in `CONFIDENCE` (`None` → `missing`, else `invalid`); on `_Rejected`
     return `ParkedBrief(exc.field, exc.reason)`; finally park
     `("body", "too-large")` when the rendered body exceeds
     `MAX_BODY_BYTES` encoded;
   - `_text(field, value)`: `None` → `missing`; non-`str` → `invalid`;
     `" ".join(value.split())`; empty → `missing`; over `MAX_TEXT` →
     `too-long`; any char in category `Cc/Cf/Cs/Co` →
     `control-character`; first matching `_REJECTIONS` category (IPv6 via
     `_has_ipv6` as `private-identifier`) → that category;
   - `_items(field, value)`: falsy → `missing`; non-list → `invalid`; over
     `MAX_ITEMS` → `too-long`; else `tuple(_text(field, item) ...)`;
   - `render_brief`: title `f"Repair: {problem}"`, cut to 117 chars +
     `...` when over 120; body sections and labels exactly as the spec's
     *Rendering* paragraph, every body source value through `_code`;
     provenance = `KEY_LINE.format(key)`, blank line, ```` ```text ````
     fence with the seven `name: value` lines, closing fence;
   - `_code(text)`: fence = one more backtick than the longest backtick run
     in `text`; return `f"{fence} {text} {fence}"`.
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
