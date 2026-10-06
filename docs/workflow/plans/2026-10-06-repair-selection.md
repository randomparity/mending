# Plan: select one repair candidate and version the reviewed brief (#22)

Goal: `repair-queue sync` creates at most one ranked small repair per run,
records a no-op otherwise, and stores a reviewed-brief version on link
records, per [the spec](../specs/2026-10-06-repair-selection-design.md) and
[ADR 0014](../../adr/0014-repair-candidate-selection.md).

Architecture: `repair_brief.py` gains `reviewed_version`; `repair_queue.py`
accepts the optional version on link records; a new
`repair_selection.py` owns `rank_key`; the command module returns small
repairs from `_sync_one` as selectable and adds `_select`, `_pending_repairs`,
and `_record_selection`, and writes the version in `_write_link`.

Tech stack: Python 3.11+, pytest, `gh` via fixed argv.

Expected implementation size: 330–430 changed lines (M) — about 130
production lines (command ~85, brief ~20, record shape ~10, ranking ~20,
docs ~15) and 200–300 test lines (new selection tests plus link-record
assertion updates in existing tests).

## Global Constraints

- No new dependencies. Engine modules must not import `desloppify.app`.
- Fail closed: an unrecognized pending record blocks selection; a malformed
  `reviewed_brief_version` raises `RepairRecordError`. Printed reasons are
  fixed strings plus work-item ids and issue numbers, never brief text.
- Do not touch `desloppify/app/commands/update_skill/`, `repair_cycle*`, or
  ADRs 0001–0013. No ADR index exists.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## File map

| File | Change | Owns after |
|---|---|---|
| `desloppify/engine/repair_brief.py` | add `REVIEWED_SCHEMA`, `reviewed_version` | brief and its two versions |
| `desloppify/engine/repair_queue.py` | `_kind_fields` keeps `reviewed_brief_version` | record shapes |
| `desloppify/engine/repair_selection.py` | new: `rank_key` | ranking order |
| `desloppify/app/commands/repair_queue.py` | `_sync`, `_sync_one`, `_preview_proposal`, `_create_once`, `_write_link`; new `_select`, `_safe`, `_pending_repairs`, `_record_selection` | sync flow |
| `desloppify/tests/repair_queue/test_selection.py` | new | Success 1–6 |
| `desloppify/tests/repair_queue/test_promotion.py` | merge/shape test | Success 7 |
| existing tests asserting whole link records | include the version | — |
| `README.md`, `desloppify/app/cli_support/parser_groups_repair_queue.py` | prose | — |

## Task 1 — Version and ranking primitives

Files: `repair_brief.py`, `repair_queue.py`, new `repair_selection.py`;
tests in `test_selection.py` and `test_promotion.py`.

Interfaces (later tasks rely on):
`reviewed_version(issue: Mapping[str, Any], candidate: PromotionCandidate) -> str | None`;
`rank_key(issue: Mapping[str, Any], candidate: PromotionCandidate) -> tuple[int, int, int, int, str]`.
Borrowed, confirmed present: `build_brief`, `ParkedBrief`, `_manifest(detail)`
(repair_brief); `matching_record`, `_is_digest`, `RepairRecordError`
(repair_queue); `upsert_issues` (`engine/_state/merge_issues.py`).

Verification:
- `reviewed_version` stable under wording — Mode: focused-test.
  `test_reviewed_version_ignores_wording_and_tracks_material`: editing
  `summary`/`maintenance_consequence` keeps it; editing
  `protected_contracts` or the manifest blob ID moves it; a parked brief gives
  `None`. Red: `ImportError`. Green:
  `uv run pytest -q desloppify/tests/repair_queue/test_selection.py -k reviewed_version`.
- Record shape — Mode: focused-test. `test_link_version_survives_merge_and_rejects_malformed`
  in `test_promotion.py`: `normalize_record` keeps a digest version, raises on
  `"x"`; `upsert_issues` keeps it. Red: version dropped. Green:
  `uv run pytest -q desloppify/tests/repair_queue/test_promotion.py -k link_version`.
- Ranking — Mode: focused-test. `test_rank_key_orders_risk_feasibility_evidence_benefit`:
  fewer files first; then finding before concern; then more citations; then
  more cited lines; then key. Red: `ImportError`. Green: `-k rank_key`.

Steps:
1. Write the three tests; run; expect the red above.
2. `repair_brief.py`:
   ```python
   REVIEWED_SCHEMA = "desloppify-reviewed-brief:v1"
   _WORDING = frozenset({"problem", "consequence", "revision", "manifest_digest"})

   def reviewed_version(issue, candidate) -> str | None:
       """Digest of material brief fields and source blobs; ``None`` when the brief parks."""
       brief = build_brief(issue, candidate)
       if isinstance(brief, ParkedBrief):
           return None
       fields = {k: v for k, v in asdict(brief).items() if k not in _WORDING}
       sources = [[d.path, d.role, d.object_id] for d in _manifest(issue["detail"]).dependencies]
       record = {"schema": REVIEWED_SCHEMA, "brief_schema": brief.schema, **fields,
                 "sources": sources}
       return sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
   ```
3. `repair_queue._kind_fields`, link branch: build `fields` with number/url/state;
   if `"reviewed_brief_version" in record`, require `_is_digest` else raise
   `RepairRecordError(f"{kind} has an invalid reviewed brief version")`, then copy it.
4. `repair_selection.py`:
   ```python
   def rank_key(issue, candidate) -> tuple[int, int, int, int, str]:
       """Risk, verification feasibility, evidence, benefit, then key; lowest ranks first."""
       record = matching_record(issue["detail"], "github_repair_revalidated", candidate) or {}
       citations = [c for claim in record.get("check", {}).get("claims", [])
                    for c in claim["citations"]]
       return (
           len(record.get("manifest", {}).get("dependencies", [])),
           0 if candidate.route == "finding" else 1,
           -len(citations),
           -sum(c["end"] - c["start"] + 1 for c in citations),
           candidate.key,
       )
   ```
5. Run the three green commands, then `make lint typecheck arch`; commit
   `feat: add reviewed brief version and repair ranking key`.

## Task 2 — Selection in sync and versioned link writes

Files: `desloppify/app/commands/repair_queue.py`; tests in `test_selection.py`
plus updates to whole-record assertions in `test_repair_queue.py`,
`test_promotion.py`, and `test_sync_matrix.py`.

Interfaces: consumes `reviewed_version`, `rank_key`; `_create_once` now
returns `bool` (True once the pending record is written).

Verification (each Mode: focused-test, file `test_selection.py`, green
`uv run pytest -q desloppify/tests/repair_queue/test_selection.py`; red is
the current behavior named):
- `test_three_candidates_create_only_the_rank_first` — red: three creates.
- `test_clean_state_records_no_op_and_dry_run_records_nothing` — red: no
  `repair_queue_selection` key.
- `test_pending_repair_blocks_selection` — red: a create happens; asserts the
  printed line names the blocking item and its key, and a non-mapping pending
  prints the reconcile-manually line.
- `test_human_edited_and_closed_links_are_kept_and_another_is_selected` and
  `test_dismissed_item_is_never_selected` — red: the version assertion on the
  read-back link fails.
- `test_uncertain_create_keeps_pending_and_next_run_creates_nothing` — red:
  no recorded selection.
- `test_material_change_reports_changed_and_wording_change_does_not` — red:
  no version on the link, no `Changed` line.

Steps:
1. Write the tests with a three-item fixture (two concerns differing in
   citation count, one `dupes` pair) built like `test_repair_queue._state`; run; expect red.
2. `_sync`: collect `selectable` from `_sync_one(...)` returning `True`; call
   `_select(args, client, selectable)` after the loop.
3. `_sync_one`: every existing `return` becomes `return False`; the final
   branch becomes `elif candidate.kind == "proposal":` create-or-preview
   (unchanged), `else: return True`; end with `return False`.
   `_preview_proposal` keeps only the park and proposal messages.
4. Add `_select`, `_safe`, `_pending_repairs`, `_record_selection` exactly as
   the spec's `_select` section states (messages verbatim; `at` from
   `datetime.now(UTC).isoformat()`; `_record_selection` returns early without
   `--apply`).
5. `_create_once`: inside the lock, after the existing stale checks, add
   `if candidate.kind == "small_repair" and _pending_repairs(state, candidate.repository):`
   print `Skipped <id>: another repair publication is pending.` and `return
   False`; `return False` on each other early exit inside the lock, `return
   True` after it (create outcome no longer changes the result). Test:
   `test_create_once_refuses_while_another_repair_is_pending` (red: create runs).
6. `_write_link`: inside the lock read `previous = _expected_record(detail,
   lane.link, candidate)`, compute
   `version = reviewed_version(_issues(state)[candidate.issue_id], candidate)`
   (the locked work item, not the `GitHubIssue` argument),
   add it to the record when not `None`, set `changed = previous is not None and
   (previous["evidence_digest"] != candidate.evidence_digest or
   previous.get("reviewed_brief_version", version) != version)`; print the
   spec's `Changed` line when `changed`, else the existing `Linked` line.
7. Update existing whole-record assertions to include
   `"reviewed_brief_version": ANY` where the fixture brief builds; run
   `uv run pytest -q desloppify/tests/commands/test_repair_queue.py desloppify/tests/repair_queue`
   — expect all pass. Commit `feat: select one repair candidate per sync`.

## Task 3 — Docs

Mode: task-test-not-applicable — README and argparse help are prose with no
executable consumer. Edit README's repair-queue paragraph (both routes,
one-per-run selection, recorded no-op, non-dispatchable proposals) and drop
the #18 entry from "Planned"; change the `repair-queue` parser help to
`Publish revalidated small repairs (one per sync) and non-dispatchable proposals to GitHub`
and give the `sync` subparser `help=` and `description=`: `Reconcile linked
issues, then publish at most one ranked small repair (a high-confidence concern
in one directory of at most 3 files, or an exact same-file duplicate pair);
records a no-op when none is safe. Proposals publish without status:ready and
are never dispatched.`
Run `make tests`; commit `docs: describe repair selection and proposal path`.

## Final

Run every guardrail in Global Constraints bare; all exit 0.
