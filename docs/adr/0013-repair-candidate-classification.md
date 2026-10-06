# 0013 — Classify repair candidates into small repairs and proposals

## Status

Accepted (2026-10-05)

## Context

ADRs 0005 and 0008 admit only current `concerns` work items to the repair
queue, and every admitted item is published as a `status:ready` repair issue
carrying the concern-key line (ADR 0009's `desloppify-repair-brief:v1`).
ADR 0012 proves a concern's source by its `PATH:LINE` evidence anchors and
rejected re-running detectors for concerns. #18/#21 ask for a second route
for bounded mechanical findings and for uncertain or cross-boundary work to
become a human-decided proposal that never enters the dispatch queue. A
mechanical finding has no `concern_identity` or `concern_evidence_digest`;
those are set only by review import. The operator decided on 2026-10-05 that
proposals are published to GitHub as non-dispatchable issues.

## Decision

- **Typed classification.** Eligibility returns `small_repair`, `proposal`, or
  `ineligible` with a reason, over two explicit routes: `concerns` (ADR
  0004/0008/0012 gates, then predicates) and mechanical findings. Every other
  detector — formatter, dependency, security, and the remaining mechanical
  detectors — is `ineligible`; adding one is a new decision.
- **Small-repair predicates.** A concern is a small repair only when its
  manifest files number at most 3, sit in one directory, it carries a
  verification plan, and its review confidence is `high`; any failure makes
  it a proposal whose open questions are the failures. A mechanical finding
  is a small repair only when it is an exact two-member duplicate pair inside
  one file (`dupes`); otherwise it is ineligible, never a proposal.
- **Finding identity.** A finding's identity hashes detector, file, and the
  sorted function names; its evidence version hashes kind, names, lines, and
  sizes. Its key is a distinct schema, `desloppify-finding-key:v1`, with its
  own body line. A finding is never given concern fields or the concern key.
- **Finding source proof.** Revalidation binds the finding file in a complete
  manifest and stores a `desloppify-repair-check:v1` result whose claims are
  the finding's own anchors (each function's line range and name) instead of
  parsed evidence text, plus a span check that each name is on its span's
  first line and the spans' remaining lines are equal after stripping
  whitespace. Recheck, transient handling, and invalidation follow ADR 0012
  unchanged. A finding's repair brief keeps `desloppify-repair-brief:v1`
  and its fields, with fixed guidance text and `finding-key` provenance
  labels.
- **Proposal lane.** A proposal is published with no label, a
  `desloppify-proposal-key` line carrying a one-way marker (SHA-256 of
  `desloppify-proposal-key:v1` and the repair key) and no concern identity,
  so a proposal body never discloses the repair key before a repair exists,
  and a `desloppify-proposal-brief:v1` body
  (observed evidence, alternatives and trade-offs, ownership and contracts,
  open questions, decision needed). It is recorded only as `github_proposal`
  or `github_proposal_pending`. Sync adopts a GitHub issue only when its body
  carries the lane's own line, adopts a peer item's link only from the same
  lane, skips an item holding its other lane's record, and creates nothing
  while any record of either lane exists, so a proposal is never linked,
  labeled, or adopted as a repair. `recover` clears either lane's pending
  record. Approving
  a proposal leads only to a separately scoped, revalidated execution
  decision outside this command.

ADRs 0005, 0008, 0009, and 0012 otherwise stay in force and are not edited;
readers find the widened eligibility, the second key schema, the second check
input, and the proposal variant here.

## Consequences

Medium- and low-confidence or multi-directory concerns now publish as
proposals instead of repairs. A repair issue published before this change, or
before a reclassification, is not withdrawn; the dispatch-time recheck
(#19/#26) and selection (#22) own that. A renamed file gives a duplicate pair
a new identity. Repair-lane adoption still trusts any body carrying the key
line without an author check (ADR 0008); this change adds no earlier
disclosure of that key. A lane flip while the other lane holds a record
parks the item until a human reconciles it. `candidate_from_issue` keeps its
meaning, a current small-repair candidate, so any caller of it never sees a
proposal; sync reads both kinds through `classify`.
The dupes detector keeps the signature line when it normalizes a body, so
an exact pair shares a name (same-name methods in different scopes of one
file); its identity cannot tell two such pairs in one file apart, as the
existing dupes work-item ID already cannot, and a pair whose raw bodies
differ only in comments fails the span check. Proposal publication is not
rate-limited; #22 owns selection. Scan merge now
preserves queue records for findings too. The brief record gains a `route`
field, so every v1 brief-version moves once with this change, and v1
provenance carries `concern-` or `finding-` labels by route; ADR 0009 left
the version unpersisted, so #22 starts comparing from this shape.

## Considered & rejected

- **Relabel a qualifying finding as a concern.** judgment: fit; #18 forbids
  fabricating a concern to pass the filter, and it would collide two key
  spaces.
- **Re-run the detector at revalidation.** verified: `rg -n "def detect_"
  desloppify/engine/detectors/dupes.py` at `0fa489a8` shows
  `detect_duplicates(functions: list[FunctionInfo], ...)` (line 415), which
  needs a language extractor's function inventory rather than recorded
  blobs; the anchor check is bounded and needs neither.
- **Publish mechanical proposals.** judgment: fit; findings carry no owner,
  contracts, or alternatives, and every large file would open a proposal —
  the mass-cleanup route #21 excludes.
- **Keep proposals local only.** judgment: fit; operator decision on
  2026-10-05 requires publication as non-dispatchable issues.
- **Add a `proposal` label.** judgment: cost; it adds a label-provisioning
  prerequisite, while the absent `status:ready` label and the distinct body
  line already keep a proposal out of the queue.
- **Prove a finding by its anchors alone.** judgment: fit; names and line
  ranges still hold after one function is reduced to a call of the other,
  so a removed duplicate would publish (constructed in design review).
- **Admit `orphaned`, `single_use`, or `structural`.** judgment: fit;
  deleting an orphan, inlining across areas, or splitting a large file is not
  provable as small from bounded anchors.
- **Do nothing.** judgment: fit; #21's criteria need both routes.
