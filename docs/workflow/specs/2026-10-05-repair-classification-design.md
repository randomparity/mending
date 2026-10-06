# Classify repair candidates into small repairs and proposals

Issue: #21 (part of #18). Builds on the stable key (#23), source manifest
(#24), source-bound revalidation ([ADR 0008](../../adr/0008-source-bound-repair-promotion.md)),
the sanitized brief ([ADR 0009](../../adr/0009-sanitized-repair-brief.md)), and
the evidence-anchor check ([ADR 0012](../../adr/0012-bounded-evidence-check.md)).
Decision record: [ADR 0013](../../adr/0013-repair-candidate-classification.md).

## Problem

`candidate_from_issue` (`desloppify/engine/repair_queue.py`) admits only open
`concerns` items, and every admitted item becomes a dispatchable
`status:ready` repair issue. A small, well-evidenced mechanical finding has no
route, and nothing separates a bounded repair from work that needs a human
decision.

## Scope

**Classification.** `classify(issue, repository, *, require_revalidation=True)`
returns `Classification(kind, reason, candidate)` with `kind` one of
`small_repair`, `proposal`, `ineligible`; `candidate` is `None` exactly when
`ineligible`. `candidate_from_issue` keeps its meaning and returns the
candidate only for `small_repair`; sync reads both kinds through `classify`.
Routes, chosen by `detector` on an open item with a string `id` and mapping
`detail`:

| Detector | Route | Outcome |
|---|---|---|
| `concerns` | concern | ADR 0004/0008/0012 gates unchanged; then predicates → `small_repair` or `proposal` |
| `dupes` | finding | evidence shape + predicates → `small_repair`, else `ineligible` |
| any other | — | `ineligible` ("detector has no repair route") |

**Concern predicates** (`concern_failures(issue)`, each failure a fixed
sentence; any failure → `proposal`): at most 3 files in
`concern_dependencies(issue)` (scope bound); all of them in one directory
(single ownership boundary); `detail.verification` a non-blank string
(verification present); work-item `confidence == "high"` (certainty).
Verification presence is already guaranteed upstream — review import rejects
a confirmed concern without it (`contracts_validation.py`) and it feeds the
evidence digest — so that predicate is a fail-closed guard; if it fires on
hand-edited state, the proposal brief parks on the missing field.

**Finding route (`dupes`).** Evidence shape: `detail.fn_a` and `detail.fn_b`
are mappings with string `file` and `name` and integer `line` ≥ 1 and `loc`
≥ 1, and the item's `file` is a non-empty path; else `ineligible`
("finding evidence is malformed"). Predicates (any failure → `ineligible`;
a mechanical finding never becomes a proposal): `detail.kind == "exact"`,
`detail.cluster_size == 2`, `fn_a.file == fn_b.file`. A finding is never
given concern fields or the concern key.

**Finding identity and key.** identity = SHA-256 of canonical JSON
`{"schema": 1, "detector": "dupes", "file": <item file>, "names": sorted names}`;
evidence version = SHA-256 of `{"schema": 1, "kind", "functions": sorted
[name, line, loc]}`; key = SHA-256 of
`"desloppify-finding-key:v1\n<repository>\n<identity>"`. The body key line
is `<!-- desloppify-finding-key: KEY -->`. Records reuse ADR 0008's shape
with this key; no legacy marker exists for findings.

**Source proof.** Revalidation is unchanged (complete manifest of the item
file, stored check) except that a finding's check is
`check_finding`: one claim per function citing `file:line..line+loc-1` with
the function name's last dotted segment as the quoted identifier, evaluated
by ADR 0012's predicate, bounds and schema, plus two span checks over the
same blob: each name occurs on its span's first line, and the two spans'
remaining lines are equal after stripping surrounding whitespace (the
duplication still exists). A failed span check is `fail`.

**Lanes.** A `small_repair` uses the existing records
(`github_repair`, `github_repair_pending`), key line, and `status:ready`
label. A `proposal` uses `github_proposal`/`github_proposal_pending`, the
line `<!-- desloppify-proposal-key: KEY -->`, and is created with no label.
Sync verifies and adopts only bodies carrying the lane's own line, so a
proposal issue is never linked as a repair and vice versa; any record of
either lane on the item blocks a create. An item holding a record of its
other lane is skipped with a reason naming that, and a peer item's record is
adopted only when it is the candidate's own lane link; any other peer record
parks the candidate. `recover` clears a matching pending record of either
lane. Scan merge preserves both lanes' records for concerns and findings
while identity is unchanged.

**Proposal brief** (`desloppify-proposal-brief:v1`, `ProposalBrief`): the
repair brief's sanitized fields plus `questions` = the concern's predicate
failures. Rendered title `Proposal: <problem>`; body sections: not-dispatchable
notice, observed source, observed evidence (reviewer assertions), alternatives
and trade-offs (reviewer suggestion when present, split into small repairs,
keep and dismiss — fixed text), expected ownership and contracts, open
questions, decision needed, provenance (proposal line and schema block).
A finding brief keeps schema `desloppify-repair-brief:v1` and its field set;
it fills owner, consequence, fix, contracts and verification with fixed text
and its evidence from the anchors, renders them under a detector-finding
heading, and labels provenance `finding-key`/`finding-identity`.

Out of scope: selection (#22), dispatch (#19), brief schema (#20).

## Failure model

1. **Actors and deployments** — a local operator running `repair-queue`
   `revalidate`/`sync`/`recover` (or the systemd timer) against one state
   file and one GitHub repository; imported review output and scan output
   are untrusted.
2. **Invariants and assets** — a proposal never carries `status:ready` or a
   repair key line and is never linked as `github_repair`; a mechanical
   finding never enters the concern key space; public text passes ADR 0009's
   sanitizer; at most one GitHub issue per key and lane (ADR 0005).
3. **Accepted failure classes** — a renamed file gives a dupe pair a new
   identity (bounded: one extra issue; old one stays linked by number);
   a repair issue published before reclassification to proposal stays open
   until #22/#19 recheck (bounded: `candidate_from_issue` already rejects a
   proposal); a lane flip while the other
   lane holds a record parks the item with no create until a human
   reconciles it (bounded: no duplicate issue); predicate
   thresholds may misroute borderline concerns to proposal (cost: human reads
   it).
4. **Covered elsewhere** — selection and at-most-one (#22), dispatch-time
   eligibility recheck (#19/#26), sanitizer misses (ADR 0009).

## Threat model

- **Boundaries.** Widened: scan output (`dupes` detail) now reaches the
  public issue body; review output reaches a new public proposal body. Added:
  none (same `gh` adapter, same repository identity check).
- **Actors.** Untrusted: whoever wrote the scanned source (function names,
  paths) and the model that wrote review text. Trusted: the operator and the
  installed `gh` credentials.
- **Controls.** Every published value goes through ADR 0009's `_text`
  sanitizer and code-span rendering; dupe evidence uses the item's relative
  `file`, never the raw detector `fn_*.file` (which may be absolute); shape
  checks reject non-string/non-positive anchors before use; `create` keeps
  fixed argv. A proposal's labels are a fixed empty set.
- **Out of scope.** Sanitizer pattern misses (ADR 0009); label or body edits
  by humans after publication (#22).

## Success

1. The four fixtures classify as specified: exact same-file dupe pair →
   `small_repair`; bounded high-confidence concern → `small_repair`;
   malformed or cross-file/non-exact dupe, `unused`, `structural`,
   `security` → `ineligible`; multi-directory concern → `proposal`.
2. Sync `--apply` on a proposal calls create with no label and a body with no
   `desloppify-concern-key`/`desloppify-finding-key` line, records only
   `github_proposal*`, and a repair-lane sync never adopts that issue.
3. A revalidated dupe pair publishes a repair issue carrying the finding key.

## Validation

Focused tests in `desloppify/tests/repair_queue/test_classify.py`,
`test_brief.py`, `test_check.py`, `desloppify/tests/commands/test_repair_queue.py`,
and the merge test; repository guardrails `make lint typecheck arch tests`.
