# Plan: classify repair candidates into small repairs and proposals (#21)

Goal: replace the concern-only eligibility filter with a typed classification
over a concern route and a `dupes` finding route, and publish proposals as
non-dispatchable issues, per [the spec](../specs/2026-10-05-repair-classification-design.md)
and [ADR 0013](../../adr/0013-repair-candidate-classification.md).

Architecture: `repair_queue.py` owns classification, finding identity/key,
record kinds, and the two lanes; `repair_check.py` gains a finding claim
source over the existing predicate; `repair_brief.py` gains finding fields and
`ProposalBrief`; the command module routes every record kind and create label
through the candidate's lane; scan merge preserves both lanes' records for
both routes.

Tech stack: Python 3.11+, pytest, `gh`/`git` via fixed argv.

Expected implementation size: 1,050–1,250 changed lines (M) — about 680
production lines (classification and finding identity ~270, check ~65 with
the span check, brief ~220 including the proposal renderer's fixed text and
the shared section helpers, command ~100, merge ~25) and about 550 test lines.
Corrected after the build: the first estimate (550–750) undercounted the
brief renderer, the command's lane edits, and the review-added span check,
peer and recover lanes; no work was added beyond the reviewed design.

## Global Constraints

- No new dependencies. Engine modules must not import `desloppify.app`.
- Fail closed: a malformed shape is `ineligible`; any record of either lane
  blocks a create; reasons are fixed strings and never echo untrusted text.
- Every published value passes `repair_brief._text`; finding evidence uses the
  item's relative `file`, never `fn_*.file`.
- Do not touch `repair_cycle*`, `update_skill`, `pyproject.toml`, `README.md`,
  review prompt files, `SKILL.md`, or ADRs 0001–0012 (sibling campaign work /
  immutable records).
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## Task 1 — Classification, finding identity, lanes (`desloppify/engine/repair_queue.py`)

Interfaces produced:

```python
FINDING_KEY_SCHEMA = "desloppify-finding-key:v1"
FINDING_KEY_LINE = "<!-- desloppify-finding-key: {} -->"
PROPOSAL_LINE = "<!-- desloppify-proposal-key: {} -->"
MAX_SMALL_FILES = 3

@dataclass(frozen=True)
class PromotionCandidate:  # two new trailing fields with defaults
    issue_id: str; identity: str; evidence_digest: str; key: str; repository: str
    route: str = "concern"        # "concern" | "finding"
    kind: str = "small_repair"    # "small_repair" | "proposal"

@dataclass(frozen=True)
class Classification:
    kind: str                     # "small_repair" | "proposal" | "ineligible"
    reason: str
    candidate: PromotionCandidate | None

@dataclass(frozen=True)
class Lane:
    link: str; pending: str; ready: bool

REPAIR_LANE = Lane("github_repair", "github_repair_pending", True)
PROPOSAL_LANE = Lane("github_proposal", "github_proposal_pending", False)

def classify(issue, repository, *, require_revalidation=True) -> Classification
def candidate_from_issue(issue, repository, *, require_revalidation=True) -> PromotionCandidate | None  # small_repair only
def concern_failures(issue: Mapping[str, Any]) -> tuple[str, ...]
def item_hashes(issue: Mapping[str, Any]) -> tuple[str, str, str] | None  # (route, identity, evidence)
def record_key(route: str, repository: str, identity: str) -> str
def finding_key(repository: str, identity: str) -> str
def lane_for(candidate: PromotionCandidate) -> Lane
def marker_line(candidate: PromotionCandidate) -> str
def normalize_record(kind, record, repository, identity, evidence_digest, *, route="concern")
GitHubIssueClient.create(repository, title, body, *, ready: bool = True) -> None
```

Verification:

- Mode: focused-test — classification table. `desloppify/tests/repair_queue/test_classify.py`:
  exact same-file dupe → `small_repair` with `route="finding"` and key
  `finding_key(...)`; bounded high-confidence concern → `small_repair`;
  malformed `fn_a` (missing `line`), cross-file, `kind="near"`,
  `cluster_size=3`, `detector` in {`unused`, `structural`, `security`} →
  `ineligible` with `candidate is None`; concern with related files in two
  directories and confidence `medium` → `proposal` whose
  `concern_failures` names both. Red: `ImportError: cannot import name 'classify'`.
  Green: `uv run --locked pytest desloppify/tests/repair_queue/test_classify.py -q`.
- Mode: focused-test — finding key never equals the concern key for the same
  identity and `normalize_record(..., route="finding")` rejects a `marker`
  record. Same file and command.
- Mode: focused-test — `carries_concern_marker` accepts only the lane's own
  line (proposal body with the concern key line is rejected and vice versa).
  Same file and command.
- Mode: focused-test — `create(..., ready=False)` argv has no `--label`.
  Same file and command.

Steps:

1. Write the tests above; run; expect the import error.
2. Add the constants, dataclasses, `finding_key`, `record_key`
   (`concern_key` for `"concern"`, `finding_key` for `"finding"`), and
   `_digest(obj)` = SHA-256 of `json.dumps(obj, sort_keys=True, separators=(",", ":"))`.
3. `_finding_hashes(issue)`: when `detector == "dupes"`, `file` is a non-empty
   string, and both `fn_a`/`fn_b` pass `_function(value)` (mapping with str
   `file`/`name`, non-bool int `line`/`loc` ≥ 1), return
   `(_digest({"schema": 1, "detector": "dupes", "file": file, "names": sorted names}),
   _digest({"schema": 1, "kind": detail.get("kind"), "functions": sorted [[name, line, loc]]}))`;
   else `None`. `item_hashes` returns `("concern", *concern_hashes(detail))`
   for `concerns` and `("finding", *found)` for `dupes`, returning `None`
   whenever the underlying hashes are `None` (a malformed item never raises).
4. `concern_failures(issue)`: paths from `concern_dependencies(issue) or ()`;
   append `"scope spans N files; the bound is 3"` when `len > 3`,
   `"files span N directories"` when `len({posixpath.dirname(p)}) > 1`,
   `"no verification plan"` when `detail.verification` is not a non-blank str,
   `"review confidence is not high"` when `issue.confidence != "high"`.
5. `classify`: open item with str `id` and mapping `detail`, else ineligible
   `"not an open work item"`. `concerns` → existing gates (hashes,
   no previous-digest fields, revalidation when required → ineligible
   `"no current source-bound revalidation"`), then kind `proposal` when
   `concern_failures` is non-empty. `dupes` → `_finding_hashes` is `None` →
   ineligible `"finding evidence is malformed"`; `kind != "exact"`,
   `cluster_size != 2`, or `fn_a.file != fn_b.file` → ineligible
   `"finding exceeds the small-repair bound"`; else revalidation gate, then
   `small_repair`. Other detectors → ineligible `"detector has no repair route"`.
6. `normalize_record` gains `route`; key via `record_key`; the legacy-marker
   branch applies only to `route == "concern"`. `_kind_fields` treats
   `github_proposal` like `github_repair` and `github_proposal_pending` like
   `github_repair_pending`. `matching_record` passes `candidate.route`.
7. `marker_line`: `PROPOSAL_LINE` for proposals, `FINDING_KEY_LINE` for
   findings, else `KEY_LINE`. `carries_concern_marker` expects `marker_line`
   plus the legacy line only for a concern small repair.
8. `create` appends `--label status:ready` only when `ready`.
9. Run the focused tests green, then `make lint typecheck`; commit
   `feat: classify repair candidates by route and kind`.

## Task 2 — Finding source proof (`desloppify/engine/repair_check.py`)

Interfaces: `check_finding(root: Path, manifest: SourceManifest, issue: Mapping[str, Any]) -> CheckResult`.

Verification:

- Mode: focused-test — `desloppify/tests/repair_queue/test_check.py`: a git
  fixture file holding two identical functions `alpha`/`beta` passes; a line
  range beyond the file fails `"cited line is outside the file"`; a renamed
  function fails `"quoted identifier is absent from the cited files"`; an item
  file not in the manifest is `unknown`. Red: `ImportError` for
  `check_finding`. Green: `uv run --locked pytest desloppify/tests/repair_queue/test_check.py -q`.

- Mode: focused-test — span check: a fixture whose `beta` body was reduced to
  `return alpha()` (names and ranges still valid) fails
  `"duplicate spans differ"`; a name not on its span's first line fails
  `"function name is not on its first line"`. Same command.

Steps: write tests; add `_finding_claims(issue, manifest)` returning a tuple of
`Claim(Citation(file, line, line + loc - 1), (name.rsplit(".", 1)[-1],))` per
function when the file is a present dependency, else the reason string; extract
the shared tail of `check_concern` into `_run_claims(root, manifest, claims,
extra=None)` where `extra(sources)` returns a failure reason after the claims
hold; `_CitedBlob` gains the blob `text`; `check_finding` passes `extra` =
`_span_failure(claims)` comparing the two spans; run green; commit
`feat: prove dupe findings by their anchors`.

## Task 3 — Briefs (`desloppify/engine/repair_brief.py`)

Interfaces: `PROPOSAL_SCHEMA = "desloppify-proposal-brief:v1"`;
`class ProposalBrief(RepairBrief): questions: tuple[str, ...]`;
`build_brief(issue, candidate) -> RepairBrief | ProposalBrief | ParkedBrief`;
`render_brief(brief) -> tuple[str, str]`.

Verification:

- Mode: focused-test — `desloppify/tests/repair_queue/test_brief.py`: a
  proposal renders title `Proposal: …`, contains `PROPOSAL_LINE`, the schema
  line, every open question, and the "not ready for repair" notice, and does
  not contain `desloppify-concern-key`; a finding brief contains
  `FINDING_KEY_LINE`, both `file:line` anchors, and no raw `fn_a.file`;
  a hostile `fix` still parks a proposal. Red: `ImportError` for
  `ProposalBrief`. Green: `uv run --locked pytest desloppify/tests/repair_queue/test_brief.py -q`.

Steps: first set the existing `test_brief.py` fixture's `confidence` to
`"high"` (after Task 1 a medium concern is a proposal); write tests;
`RepairBrief` gains a trailing `route: str = "concern"` field so the renderer
picks the key line; `_fields(issue, candidate)` returns the raw field mapping
(concern: as today; finding: fixed consequence, fix, contracts, verification,
owner = item `file`, evidence = `f"{file}:{line} `{name}` ({loc} lines)"` per
function); build `ProposalBrief(..., questions=concern_failures(issue))` when
`candidate.kind == "proposal"` (questions sanitized by `_items`);
`version` uses the instance's schema; `render_brief` dispatches to
`_render_proposal` for `ProposalBrief`; key line and provenance labels come
from `marker_line` and the route. Commit `feat: add proposal brief variant`.

## Task 4 — Command lanes and scan merge

Files: `desloppify/app/commands/repair_queue.py`,
`desloppify/engine/_state/merge_issues.py`.

Verification:

- Mode: focused-test — `desloppify/tests/commands/test_repair_queue.py`: a
  revalidated proposal syncs with `create(..., ready=False)`, records
  `github_proposal_pending` then `github_proposal`, never `github_repair*`;
  a repair-lane candidate whose search returns the proposal issue is skipped
  as unverified; a dupe pair revalidates through `check_finding` and creates a
  repair issue carrying `FINDING_KEY_LINE` with `ready=True`; revalidating an
  ineligible item fails naming its reason. Existing fixtures gain
  `confidence="high"` so they stay small repairs. Green:
  `uv run --locked pytest desloppify/tests/commands/test_repair_queue.py desloppify/tests/repair_queue -q`.
- Mode: focused-test — lanes outside sync's main path: `recover --marker KEY`
  removes a `github_proposal_pending` record; a repair candidate whose peer
  holds `github_proposal`, and a proposal candidate whose peer holds
  `github_repair`, are both skipped with no view, search, or create; an item
  holding its other lane's record is skipped with a reason naming it. Same
  command.
- Mode: focused-test — merge: `upsert_issues` over a rescanned dupe item
  keeps `github_repair` and `github_proposal` records while identity holds
  and drops revalidation when evidence moves. Same command.

Steps: in the command, replace literal record kinds with `lane_for(candidate)`
fields (`_sync_one`, `_adopt_if_unique`, `_create_once`, `_write_link`,
`_read_link`, and `_resolved_by_peer`, which adopts only a peer's
`lane.link`), skip an item holding its other lane's record, make
`_has_record`, `_peer_records`, and `_pending_matches` iterate both lanes
using `item_hashes`/`record_key`, make `_recover` pop the matched lane's
pending kind, list sync candidates through `classify(...).candidate` (proposals
included) while `candidate_from_issue` stays small-repair-only, choose `check_finding`
for non-concern items in `_check`, pass `ready=lane.ready` to `create`, print
`Would publish a proposal issue` for proposals, and raise
`work item is not eligible for revalidation (<reason>)` in `_revalidate`. In
merge, call `_preserve_repair_metadata(previous, issue_with_new_detail)` for
every detector, compare `item_hashes`, and include both proposal kinds. Run
all guardrails; commit `feat: route proposals outside the repair queue`.
