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

Expected implementation size: 320–400 changed lines (M) — canonical comparison,
state propagation, conservative cleanup and reconciliation, and focused fixtures.

## Task 1: Persist canonical concern comparison evidence

Files: `desloppify/engine/_concerns/`,
`desloppify/intelligence/review/importing/holistic_issue_flow.py`, and
`desloppify/intelligence/review/importing/holistic.py`, and
`desloppify/engine/_state/schema_types_review.py`.

Interfaces: a confirmed concern detail carries `concern_identity` and
`concern_evidence_digest`, each a SHA-256 hash of canonical compact JSON with
sorted keys and `schema: 1`. Identity inputs are normalized `dimension`,
`identifier`, `root_cause_cluster`, and `proposed_owner`. Evidence inputs are
normalized `maintenance_consequence`, sorted unique `protected_contracts`, and
`verification`. `protected_contracts` is the sole governing-decision fixture;
its normalized-value change represents the governing-decision change without an
ADR-file lookup or a distinct ADR-evidence surface. Path, related-file,
summary, and free-form evidence fields are excluded. Dismissal payload
normalization retains its existing fingerprint;
import resolves it to one generated concern, then stores concern-type/sorted-
detector identity and sorted suppression-fingerprint evidence. Missing or
non-unique dismissal evidence is unknown and does not suppress.

Verification:

- Mode: focused-test. Contract: a path-only rename preserves both confirmed
  hashes; each listed identity/evidence input changes its listed hash; and a
  dismissal suppresses only one unchanged generated identity/evidence pair. A
  `protected_contracts` mutation changes the governing-evidence digest.
  Cleanup retains a dismissal across a supported rename only for one current
  matching identity/evidence pair.
  Red observation: current details have no canonical comparison fields and a
  dismissal stores source IDs only. Green command:
  `uv run --locked pytest -q desloppify/tests/intelligence/test_review_import_prepare_split_direct.py desloppify/tests/detectors/test_concerns.py` exits 0.

Steps: add focused fixtures; add the narrow canonicalization helper at the
  concern boundary; persist confirmed hashes; derive dismissal hashes from its
  surviving fingerprint after normalization; retain stale-source dismissals only
  for exactly one current matching comparison; require exactly one matching
  stored dismissal; run the focused tests.

Acceptance: persisted concern data can distinguish identity from freshness
without depending on a file-addressed work-item ID.

## Task 2: Reconcile one proven successor conservatively

Files: `desloppify/engine/_plan/scan_issue_reconcile.py`,
`desloppify/engine/_plan/schema/__init__.py`, and
`desloppify/tests/plan/test_reconcile.py`.

Interfaces: reconciliation accepts one old/new concern pair only when the
canonical identity and evidence digest match and both items use the `concerns`
detector. It replaces the old ID in
`queue_order`, `skipped`, `overrides`, cluster `issue_ids`, action `issue_refs`,
and `promoted_ids`, then writes `status: remapped` and `remapped_to`. A
same-identity changed digest uses ordinary supersession with
`revalidation_reason: concern_evidence_changed` and the successor candidate.
Missing evidence, failed analysis, or multiple candidates records no transfer
and no revalidation reason.

Verification:

- Mode: focused-test. Contract: one supported rename remaps each named plan
  collection; ambiguity and missing evidence retain ordinary supersession;
  changed evidence persists the revalidation reason without transferring a
  reference; a non-concern candidate with matching hashes is not a successor.
  Red observation: current reconciliation selects candidates by detector and
  file only. Green command:
  `uv run --locked pytest -q desloppify/tests/plan/test_reconcile.py desloppify/tests/plan/test_epic_triage_reconcile_and_migration.py` exits 0.

Steps: write rename, ambiguity, changed-evidence, missing-evidence, and
mixed-detector fixtures; add conservative comparison and targeted reference
replacement; record the changed-evidence reason only on a unique same-identity
successor; run the focused tests. Add the optional `SupersededEntry`
revalidation-reason field needed to persist that plan record.

Acceptance: the plan never gains a successor reference from an ambiguous or
stale comparison.

## Task 3: Verify the integrated state transition

Files: the Task 1–2 files and their tests only.

Interfaces: confirmed-import evidence reaches reconciliation without changing
the existing review discovery, GitHub queue, or scheduler contracts.

Verification:

- Mode: focused-test. Contract: an unchanged recheck preserves the plan
  disposition; a changed pair preserves the downstream revalidation reason;
  an unchanged dismissal remains hidden and a changed dismissal is visible.
  Red observation: current imports and reconciliation do not share comparison
  evidence. Green command: `uv run --locked pytest -q desloppify/tests/intelligence/test_review_import_prepare_split_direct.py desloppify/tests/plan/test_reconcile.py` exits 0.

Steps: execute the focused integration fixtures; inspect the narrow diff; run
`make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and `make tests`;
expect each command to exit 0; commit verification-driven corrections.

Acceptance: the branch proves the requested preservation and invalidation paths
without broadening into the excluded owners' surfaces.
