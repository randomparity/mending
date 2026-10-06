# Select one repair candidate and version the reviewed brief

Issue: #22 (part of #18). Builds on ADRs 0005, 0008, 0009, and 0013 and the
classification spec (`2026-10-05-repair-classification-design.md`).
Decision record: [ADR 0014](../../adr/0014-repair-candidate-selection.md).

## Problem

`_sync` (`desloppify/app/commands/repair_queue.py`) creates one GitHub issue
for every eligible candidate in a run, with no ranking, no bound, and no
record when nothing qualifies. Link records keep the evidence digest but no
reviewed-brief version, so #26 has nothing to bind execution approval to.

## Scope

**Sync flow.** `_sync` keeps its candidate list and ambiguity skip.
`_sync_one` keeps every reconciliation branch unchanged; only its final
create branch changes: a proposal still previews or creates there; a small
repair is returned to `_sync` as selectable instead. After the loop,
`_select(args, client, selectable)` runs once.

**`_select`.** Re-reads state (`_read_state`). Outcome, first match wins:

1. Any work item's `detail.github_repair_pending` is not `None` and is either
   not a mapping or names this repository → no-op, reason
   `unresolved repair publication`; print, per blocking item,
   `No repair selected: repair publication for <id> is unresolved; check GitHub, then run repair-queue recover <key>.`
   (`<key>` is the record's `key` or legacy `marker`), or for a non-mapping
   record `No repair selected: the pending repair record on <id> is unrecognized; reconcile it manually.`
2. For each selectable candidate, `build_brief`; a parked one prints the
   existing park line (`Would park …` dry, `Parked …` with `--apply`) and is
   dropped. None left → no-op, reason `no safe small repair`; print
   `No repair selected: no safe small repair.`
3. Otherwise rank (below); print `Deferred <id>: <chosen id> ranked first.`
   for each other. Dry run prints `Would create a repair issue for <id>.`;
   `--apply` calls `_create_once(args, client, chosen)`, which now returns
   `True` when it wrote the pending record. Inside its lock, for a small
   repair, it also refuses (prints `Skipped <id>: another repair publication
   is pending.`, returns `False`) when `_pending_repair` holds, so concurrent
   syncs sharing the state file cannot both create. `False` → no-op, reason
   `selected candidate changed before create`; `True` → `selected`.

With `--apply` only, `_record_selection` writes under the state lock
`state["repair_queue_selection"][repository] = {"outcome", "reason",
"issue_id", "at"}` (`issue_id` is `None` for a no-op; `at` is UTC ISO-8601).

**Ranking** (`desloppify/engine/repair_selection.py`, `rank(entries)` over
`(issue, candidate)` pairs, ascending by `rank_key`):
`(file_count, 0 if route == "finding" else 1, -citations, -cited_lines, key)`,
read from `matching_record(detail, "github_repair_revalidated", candidate)`:
`file_count` = manifest dependencies; `citations` = all claim citations in
the stored check; `cited_lines` = Σ `end - start + 1` over them.

**Reviewed brief version** (`repair_brief.reviewed_version(issue, candidate)
-> str | None`): `None` when `build_brief` parks; else SHA-256 of canonical
JSON `{"schema": "desloppify-reviewed-brief:v1", "brief_schema": brief.schema,
**asdict(brief) minus problem/consequence/revision/manifest_digest,
"sources": [[path, role, object_id] per manifest dependency]}`.

**Link records.** `normalize_record` keeps an optional
`reviewed_brief_version` on `github_repair`/`github_proposal` (a 64-hex digest;
anything else raises `RepairRecordError`), so scan merge preserves it.
`_write_link` computes the version under the lock and writes it (omitted when
`None`). If the previous link of that lane exists and its evidence digest
differs, or it carried a version different from the new value, it prints
`Changed <id>: issue #<n> evidence or reviewed brief changed materially; an
approval bound to the earlier version no longer applies.` instead of
`Linked …`.

**Docs.** README repair-queue paragraph and the parser help (top-level and
the `sync` subcommand) describe both
eligibility routes (concerns; exact same-file `dupes` pairs), the one-per-run
selection with recorded no-op, and the non-dispatchable proposal path; the
README "Planned" line drops the #18 brief/proposal entry.

No change to: classification, keys, markers, search, adoption, recover,
`revalidate`, the brief body, or proposal publication.

## Failure model

1. **Actors and deployments** — a local operator running `repair-queue sync`
   (or the systemd timer) on one state file against one repository; imported
   review and scan output are untrusted.
2. **Invariants and assets** — at most one new repair issue per sync run
   (one state file, ADR 0005); no issue created while an earlier repair
   create is pending; operator-edited, closed, and dismissed issues stay
   linked and are never recreated; a material evidence or brief change moves
   the stored version; a wording-only change creates nothing.
3. **Accepted failure classes** — a race that changes the selected item under
   the lock yields a no-op this run (bounded: next run selects again); an
   unrecoverable pending record halts new repairs until `recover` (intended);
   the GitHub body lags a material change (bounded: approval binds the stored
   version, #26); the stored version is the last successful link write, so a
   linked item sync no longer reaches (reclassified to proposal, revalidation
   dropped, ambiguous key) keeps its earlier version — #26 must recompute
   `reviewed_version` from the current work item and refuse when it parks or
   cannot be built (ADR 0013's reclassified-repair case, owned by #19/#26); weak benefit signal for single-line concern citations
   (cost: ordering only).
4. **Covered elsewhere** — approval binding and recheck (#26); one active
   repair, PR observation, dispatch (#19/#31); body edits/live publication
   (#7); sanitizer misses (ADR 0009).

## Threat model

- **Boundaries.** None added or widened: the same `gh` adapter and fixed
  argv; the new state fields hold digests, ids, and fixed reason strings.
- **Actors.** Untrusted: review/scan authors and anyone who can open an issue
  (their bodies reach search). Trusted: the operator and `gh` credentials.
- **Controls.** Selection reads only already-validated revalidation records;
  `reviewed_brief_version` is shape-checked on load; ranking never reads
  issue bodies.
- **Out of scope.** Issue authors racing a finding-key match (ADR 0013).

## Success

1. Three selectable small repairs → exactly one `create`; the chosen one is
   the rank-first; the others print `Deferred`; `selected` is recorded.
2. Clean state (no candidates) with `--apply` → no create, `no-op` recorded
   with reason `no safe small repair`; dry run records nothing.
3. A pending repair record on any item → no create, no-op `unresolved repair
   publication`.
4. A human-edited linked issue (body without marker) and a closed linked
   issue are read back by number, not recreated, and another candidate is
   still selected; a dismissed work item is never selected.
5. Uncertain create (adapter raises) keeps the pending record; the next run
   creates nothing.
6. Material change (contracts or evidence edited, revalidated) → link's
   version changes and the `Changed` line prints; wording-only change
   (`summary` edited) → version unchanged, `Linked`, zero creates.
7. `reviewed_brief_version` survives scan merge; a malformed one parks the
   item as unrecognized.

## Validation

Each Success item is a focused test in
`desloppify/tests/repair_queue/test_selection.py` (sync-level, injected
client/source as in `test_repair_queue.py`) except ranking-key order, tested
directly in the same file, and item 7 in `test_promotion.py`. README and
parser help: `task-test-not-applicable` — prose with no executable consumer.
