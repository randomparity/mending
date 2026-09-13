# Revalidate architectural concern evidence

Persist and compare the semantic evidence of confirmed concerns so a supported
rename can retain one disposition while changed or ambiguous evidence cannot.
The change extends the current concern import, persisted detail, and plan
reconciliation paths; it does not alter concern discovery, GitHub work, or the
execution scheduler. Python 3.11+ and the locked `uv` toolchain apply.

## Global Constraints

- Keep legacy state readable; absent comparison evidence is unknown, not proof.
- Reuse the existing state and plan reconciliation boundaries.
- Touch only ADR 0004, concern identity/state/reconciliation modules, and fixtures.

Expected implementation size: 180–250 changed lines (M) — canonical comparison,
state propagation, reconciliation, and focused fixtures.

## Task 1: Persist canonical concern comparison evidence

Files: `desloppify/engine/_concerns/`,
`desloppify/intelligence/review/importing/holistic_issue_flow.py`, and
`desloppify/engine/_state/schema_types_review.py`.

Interfaces: a confirmed concern detail carries `concern_identity` and
`concern_evidence_digest`; a dismissal carries the same comparison inputs or an
explicit unknown shape. The identity excludes path-only fields. The digest
contains the stable governing claim fields defined by ADR 0004.

Verification:

- Mode: focused-test. Contract: equivalent semantic concerns with renamed paths
  compare equal, and a governing-field change alters only the evidence result.
  Red observation: current details have no canonical comparison fields. Green
  command: `uv run --locked pytest -q desloppify/tests/intelligence/test_review_import_prepare_split_direct.py desloppify/tests/detectors/test_concerns.py` exits 0.

Steps: add focused fixtures; add the narrow canonicalization helper at the
concern boundary; persist its outputs for confirmed imports and dismissals; run
the focused tests.

Acceptance: persisted concern data can distinguish identity from freshness
without depending on a file-addressed work-item ID.

## Task 2: Reconcile one proven successor conservatively

Files: `desloppify/engine/_plan/scan_issue_reconcile.py` and
`desloppify/tests/plan/test_reconcile.py`.

Interfaces: reconciliation accepts one old/new concern pair only when the
canonical identity and evidence digest match. It rewrites the existing plan
references through the current supersession/remap path. A changed digest,
missing evidence, failed analysis, or multiple candidates records no transfer.

Verification:

- Mode: focused-test. Contract: a unique supported rename remaps a plan
  reference; ambiguous and changed-evidence fixtures preserve the old
  reference and mark it stale. Red observation: current reconciliation
  selects candidates by detector and file only. Green command:
  `uv run --locked pytest -q desloppify/tests/plan/test_reconcile.py desloppify/tests/plan/test_epic_triage_reconcile_and_migration.py` exits 0.

Steps: write rename, ambiguity, changed-evidence, and missing-evidence fixtures;
add the conservative candidate comparison; route only the unique equal pair
through the existing plan-reference update; run the focused tests.

Acceptance: the plan never gains a successor reference from an ambiguous or
stale comparison.

## Task 3: Verify the integrated state transition

Files: the Task 1–2 files and their tests only.

Interfaces: confirmed-import evidence reaches reconciliation without changing
the existing review discovery, GitHub queue, or scheduler contracts.

Verification:

- Mode: focused-test. Contract: an unchanged recheck preserves the disposition,
  while a changed evidence fixture requires revalidation before later consumers
  can use it. Red observation: current imports and reconciliation do not share
  comparison evidence. Green command: `uv run --locked pytest -q desloppify/tests/intelligence/test_review_import_prepare_split_direct.py desloppify/tests/plan/test_reconcile.py` exits 0.

Steps: execute the focused integration fixtures; inspect the narrow diff; run
`make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and `make tests`;
expect each command to exit 0; commit verification-driven corrections.

Acceptance: the branch proves the requested preservation and invalidation paths
without broadening into the excluded owners' surfaces.
