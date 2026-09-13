# Bounded external repair cycle

Run one local `repair-cycle` command from a systemd timer. The command owns
configuration, a repository lock, and scheduler-only state; an injected Adept
adapter owns external claim, execution, and merge work. Python 3.11+ and the
locked `uv` toolchain apply.

## Global Constraints

- Implement one systemd deployment recipe; do not add a daemon or scheduler abstraction.
- Use the host timezone for the configured calendar window and do not catch up missed windows.
- Default runtime is 90 minutes and default model-call budget is 100; configured values override them.
- A positive cost cap and orchestrator model are required before model work.
- Missing authority, disabled configuration, stale/unknown external state, failed proof, or budget exhaustion parks before new work.
- Adept remains the owner of claims, reviews, execution, and merge gates.

Expected implementation size: 400–550 changed lines (M) — command/configuration,
durable scheduler state, injected Adept adapter, systemd recipe, and focused fixtures.

## Task 1: Define cycle configuration and durable scheduler state

Files: `desloppify/engine/repair_cycle.py`,
`desloppify/tests/repair_cycle/test_state.py`.

Interfaces: `CycleConfig.from_mapping(mapping: Mapping[str, object]) -> CycleConfig`
validates window, runtime, call count, positive cost cap, model, and enablement.
`CycleState` records the host-local day key, active repair identity, merge count,
budget use, and parked reason. State is read and written only under the existing
state lock; it contains no copied Adept claim payload.

Verification:

- Mode: focused-test. Contract: defaults apply only to runtime/calls; a missing
  model or cost cap parks; malformed limits and day keys are rejected; a new
  host-local day resets the daily merge budget without erasing active work.
  Red observation: no cycle model exists. Green command:
  `uv run --locked pytest -q desloppify/tests/repair_cycle/test_state.py` exits 0.

Acceptance: persisted state cannot authorize model work when required operator
configuration is absent.

## Task 2: Add the one-shot reconciliation runner

Files: `desloppify/app/commands/repair_cycle.py`,
`desloppify/app/cli_support/parser_groups_repair_cycle.py`,
`desloppify/app/cli_support/parser.py`,
`desloppify/app/cli_support/parser_groups.py`,
`desloppify/app/commands/registry.py`,
`desloppify/tests/commands/test_repair_cycle.py`.

Interfaces: `AdeptCycleClient.reconcile(repository: str) -> ExistingWork`,
`AdeptCycleClient.select(config: CycleConfig) -> Selection`, and
`cmd_repair_cycle(args: argparse.Namespace) -> None` are injected boundaries.
The command takes an exclusive lock, checks disablement/window/authority/budgets,
reconciles recorded work first, and starts at most one eligible repair. It writes
the next durable state before and after external I/O; timeout, proof failure,
or unknown response parks and starts nothing else.

Verification:

- Mode: focused-test. Contract: two simultaneous invocations yield one runner;
  restart after claim/create/merge reconciles instead of duplicating; disabled,
  timeout, stale base, revoked authority, exhausted runtime/calls/cost, and
  no-work states make no selection. Green command:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_cycle.py`
  exits 0.

Acceptance: only a configured, enabled, authorized cycle may call the Adept
selection boundary, and it cannot exceed one active repair or one daily merge.

## Task 3: Ship the systemd recipe and configuration documentation

Files: `docs/systemd/mending-repair-cycle.service`,
`docs/systemd/mending-repair-cycle.timer`, `docs/systemd/repair-cycle.md`,
`desloppify/tests/ci/test_repair_cycle_recipe.py`.

Interfaces: the service invokes the installed `desloppify repair-cycle` command
with a root-owned environment file; the timer exposes an operator-editable
`OnCalendar` value and sets `Persistent=false` so missed windows are skipped.

Verification:

- Mode: focused-test. Contract: the unit invokes only the one-shot command,
  exposes the host-local calendar configuration, disables catch-up, and does
  not embed a model, token, or cost secret. Green command:
  `uv run --locked pytest -q desloppify/tests/ci/test_repair_cycle_recipe.py`
  exits 0.

Acceptance: a systemd operator can install, disable, re-enable, and inspect the
one supported scheduler without creating a resident process.
