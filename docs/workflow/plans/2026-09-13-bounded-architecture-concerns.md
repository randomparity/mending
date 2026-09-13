# Implement bounded architecture concerns

Extend the existing review-batch JSON contract so only confirmed mechanical concerns carry
the architecture evidence that later lifecycle work needs. Keep issue promotion, freshness,
and scheduling outside this branch. Python 3.11+ and the existing locked `uv` toolchain apply.

## Global Constraints

- Preserve ordinary review-issue and dismissed-concern payload compatibility.
- Keep the existing batch cap as a bounded output limit; do not derive required concerns from scores.
- Do not add GitHub writes, persistent lifecycle fields, scheduler behavior, or ADR infrastructure.

Expected implementation size: 120–190 changed lines (M) — typed payload propagation and focused contract tests.

## Task 1: Define and request confirmed-concern evidence

Files: `desloppify/intelligence/review/importing/contracts_types.py`,
`desloppify/intelligence/review/importing/contracts_validation.py`,
`desloppify/app/commands/review/batch/prompt_template.py`, and focused tests.

Interfaces: confirmed payloads retain `concern_verdict: "confirmed"` and add non-empty
`root_cause_cluster`, `maintenance_consequence`, `proposed_owner`, `protected_contracts`, and
`verification`; ordinary and dismissed payloads retain their current shape. The prompt schema
and task instructions name the same fields.

Verification:

- Mode: focused-test. Contract: a complete confirmed concern retains its marker and an incomplete
  one is skipped while a valid ordinary result in the same batch remains. Red observation: the
  current validator accepts a confirmed concern with no architecture evidence and normalization
  aborts the batch. Green command: `uv run pytest -q desloppify/tests/commands/review/test_review_batch_core_direct.py` exits 0.

Steps: write the focused failing tests; make confirmed-only validation and prompt requirements
explicit; rerun the focused test; commit the coherent contract change.

Acceptance: a reviewer can see each required field in the generated batch prompt and validator.

## Task 2: Preserve normalized evidence into holistic state

Files: `desloppify/app/commands/review/batch/core_models.py`,
`desloppify/app/commands/review/batch/core_normalize.py`,
`desloppify/intelligence/review/importing/holistic_issue_flow.py`, and focused tests.

Interfaces: normalized confirmed concerns forward their marker and required evidence; holistic
import classifies them as concern details for later #4 revalidation. Ordinary reviews forward no
new required fields.

Verification:

- Mode: focused-test. Contract: a normalized complete confirmed concern stores its marker and
  architecture evidence as a concern while ordinary and dismissed payloads stay compatible. Red
  observation: current normalization drops the marker and concern-only fields. Green command:
  `uv run pytest -q desloppify/tests/commands/review/test_review_batch_core_direct.py desloppify/tests/review/context/test_holistic_review.py` exits 0.

Steps: write the focused failing persistence assertion; propagate the typed fields through the
existing normalized model and importer; rerun both focused modules; commit the propagation.

Acceptance: stored concern details contain the evidence a later owner can revalidate.

## Task 3: Verify the branch

Files: no implementation files beyond tasks 1–2.

Interfaces: the existing repository guardrails consume the completed contract.

Verification:

- Mode: task-test-not-applicable. Surface: review summary prose. Reason: the changed behavior is
  already covered by the focused contract tests; no independent executable summary contract exists.

Steps: inspect the diff; run `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
`make tests`; expect each command to exit 0; commit only verification-driven corrections.

Acceptance: the scoped tests and repository guardrails are green.
