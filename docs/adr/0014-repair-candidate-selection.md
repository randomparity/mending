# 0014 — Select one small repair per sync and version the reviewed brief

## Status

Accepted (2026-10-06)

## Context

ADR 0005 has `repair-queue sync` handle each eligible item, so one run can
create a repair issue for every candidate. #18 requires at most one eligible
small repair per run, ranked on evidence, benefit, risk, and verification
feasibility (never a score), with a recorded no-op when nothing is safe. ADR
0009 publishes a brief-version digest but left it unpersisted; ADR 0013 left
proposal rate limits and selection to #22. #26 will bind execution approval to
a brief and evidence version, so a stored version must move on a material
change and stay put on a wording-only one.

## Decision

- **Reconcile, then select.** Sync first reconciles every candidate as today:
  link readback, pending adoption, peer links, and verified search adoption,
  so an operator-edited, closed, or dismissed issue stays linked and is never
  recreated. A small repair left with no record and no matching issue is
  *selectable*. Proposals keep ADR 0013's per-item path, unranked.
- **At most one create.** If any work item still holds a
  `github_repair_pending` record for the repository, nothing is selected: an
  uncertain earlier create must be resolved (adopted, or cleared by
  `recover`) first; `_create_once` repeats this check under the state lock.
  Otherwise selectable items whose brief parks are dropped,
  the rest are ranked, and only the first goes through ADR 0005's
  pending-before-create path. The others are reported as deferred.
- **Ranking.** Lexicographic over values the stored revalidation already holds:
  fewer manifest files (risk); a finding before a concern (verification is a
  mechanical rescan, not a reviewer plan); more source-checked citations
  (evidence); more cited lines (benefit); then the key, for determinism. No
  score, tier, or quota is an input.
- **Recorded outcome.** With `--apply`, sync writes
  `repair_queue_selection[<repository>]` = `{outcome, reason, issue_id, at}`
  with outcome `selected` or `no-op`. A dry run writes nothing.
- **Reviewed brief version.** `github_repair` and `github_proposal` links carry
  `reviewed_brief_version`: SHA-256 of `desloppify-reviewed-brief:v1` over the
  brief record minus `problem`, `consequence`, `revision`, and
  `manifest_digest`, plus each source file's path, role, and blob ID. Every
  link write recomputes it, and drops it when the brief now parks. When the
  stored version or evidence digest differs, sync reports that an approval
  bound to the earlier version no longer applies; equal values change only
  the issue state read back from GitHub, and no issue is created.

ADRs 0005, 0008, 0009, and 0013 otherwise stay in force and are not edited.

## Consequences

The queue grows by at most one repair issue per run, so a backlog drains over
several runs. One unresolved pending create stops new repairs until it is
adopted or recovered. The published issue body is not rewritten on a material
change, so the issue can lag the stored version until a later publication
owner (#7) handles edits; #26 binds approval to the stored version, not the
body. The stored version is the last successful link write: a linked item
sync no longer reaches (reclassified to proposal, revalidation dropped,
ambiguous key) keeps its earlier version, so #26 recomputes it from the
current work item rather than trusting the stored value alone. Problem and consequence rewording, renderer text, and re-revalidation at
a new commit with the same blobs do not move the version. A concern's
consequence still moves its evidence digest. A state with no work items
records no selection, because `load_state` cannot tell it from a damaged file
that fell back to an empty state, and writing would replace that file. Benefit is measured only by
cited lines, which is weak for single-line concern citations. Proposals stay
unbounded. PR state is not read here; #19 observes it.

## Considered & rejected

- **Weighted score over the four factors.** judgment: fit; it is a score
  target by another name, and its weights would be arbitrary.
- **Treat an open linked repair issue as active work that blocks selection.**
  judgment: fit; dispatch (#19/#31) owns the one-active-repair bound, and
  blocking here would stall the queue behind an issue no host has started.
- **Version the whole brief (ADR 0009's published digest).** verified:
  `RepairBrief.version` in `desloppify/engine/repair_brief.py` hashes every
  field including `problem` and `revision`, so a reworded title or a
  re-revalidation at a new commit would invalidate approval.
- **Fall back to the next candidate when the selected create is refused under
  the lock.** judgment: complexity; the race is rare and the next run selects
  again.
- **Bound proposals to one per run too.** judgment: fit; #22's criteria bound
  small repairs only, and a proposal is never dispatched.
- **Do nothing.** judgment: fit; #22's criteria require the bound and version.
