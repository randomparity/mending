# Refresh findings during active work

Allow normal scans to refresh observations during an active execution plan without
resetting the plan lifecycle. Keep state persistence ahead of plan mutation; reuse the
existing merge and reconciliation components rather than adding a refresh mode.

## Global Constraints

No dependencies, GitHub writes, agent execution, architecture grouping, freshness
revalidation, scheduler, or pilot behavior. Preserve the existing `--force-rescan`
attestation and its boundary-reset semantics. Host architecture is x86_64; no target
architecture is declared. Guardrails: `make lint`, `make typecheck`, `make arch`,
`make ci-contracts`, `make tests`, `make tests-full`, `make package-smoke`.

Expected implementation size: 140–230 changed lines (M) — derived from the preflight,
reconciliation, workflow, and focused test file map.

## File map

- `desloppify/app/commands/scan/preflight.py`: admit ordinary active-plan scans.
- `desloppify/app/commands/scan/plan_reconcile.py`: distinguish active refresh from a
  lifecycle boundary before destructive reconciliation.
- `desloppify/app/commands/scan/workflow.py`: retain save-before-reconcile ordering.
- `desloppify/tests/commands/scan/test_scan_preflight.py`: prove normal and force paths.
- `desloppify/tests/commands/scan/test_plan_reconcile_postflight_and_reconcile.py` and
  `desloppify/tests/scan/test_scan_workflow_integration_direct.py`: prove lifecycle and
  persistence contracts.

## Task 1 — Admit active-work refresh

Interfaces: `scan_queue_preflight(args) -> None` continues to reject malformed explicit
force-rescan attestation, but no longer raises for a loaded plan with pending work.

Verification:
- Contract: active queue permits a normal scan.
  Mode: focused-test. Test `test_queue_remaining_allows_scan` in
  `test_scan_preflight.py`; red observation is `CommandError`; green command is
  `uv run --locked pytest -q desloppify/tests/commands/scan/test_scan_preflight.py`.
- Contract: explicit reset retains its attestation gate.
  Mode: focused-test. Existing force-rescan tests keep their current red/green cases in the
  same command.

Steps: replace the active-queue terminal error branch with an allowed refresh progression
event, retaining the existing CI and force-rescan branches. Add the focused assertion before
the behavior change, then run the focused command and commit the task.

Acceptance: active queue data is observed without an opt-in reset; `--force-rescan` still
requires its stated attestation.

## Task 2 — Preserve active plan lifecycle after a scan

Interfaces: `reconcile_plan_post_scan(runtime) -> None` records whether the loaded plan
was active before scan mutation. While active, it performs no plan reconciliation mutation.
It calls plan reconciliation only when the loaded queue was already drained or
`runtime.force_rescan` is true.

Verification:
- Contract: an active queue retains order, clusters, overrides, and plan-start scores even
  when a queued finding becomes auto-resolved in the new state.
  Mode: focused-test. Add a fixture to
  `test_plan_reconcile_postflight_and_reconcile.py`; red observation is a changed active
  association; green command is `uv run --locked pytest -q
  desloppify/tests/commands/scan/test_plan_reconcile_postflight_and_reconcile.py`.
- Contract: a drained queue retains the existing boundary reconciliation.
  Mode: focused-test. Existing reconciliation fixtures run in that command.

Steps: derive the active-refresh predicate from the loaded plan before calling any helper
that can mutate plan references. Use it to bypass plan reconciliation during refresh; do not
allow the newly merged state to turn refresh into a boundary. Add a fixture with a queued,
clustered, and overridden finding that becomes auto-resolved, run the focused command, and
commit the task.

Acceptance: refresh does not recreate clusters, remove active associations, or reset
active-cycle scoring, while a drained queue keeps its current transition behavior.

## Task 3 — Prove persistence and partial-analysis safety

Interfaces: `merge_scan_results(runtime, issues, potentials, metrics) -> ScanMergeResult`
continues to call `save_state` before `_reconcile_plan_post_scan`.

Verification:
- Contract: a failed state save performs no plan reconciliation and leaves the prior state
  file readable with its pre-scan content.
  Mode: focused-test. Add a mocked save failure in
  `test_scan_workflow_integration_direct.py`; red observations are reconciliation called or
  changed prior durable state; green command is `uv run --locked pytest -q
  desloppify/tests/scan/test_scan_workflow_integration_direct.py`.
- Contract: partial detector output leaves unexamined open findings unresolved.
  Mode: focused-test. Exercise `merge_scan` with an absent detector in
  `desloppify/tests/state/test_state.py`; green command is `uv run --locked pytest -q
  desloppify/tests/state/test_state.py`.

Steps: first write focused tests for save ordering and partial detector coverage. Make only
the smallest implementation change required by their failure. Run both focused commands,
then run `make lint`, `make typecheck`, and `make arch` before the implementation commit.

Acceptance: no plan write follows a failed state save, and partial analysis cannot close a
finding whose detector did not run.

## Rollback

Revert the feature commit to restore the preflight gate and prior reconciliation behavior;
the change introduces no persisted schema or migration.
