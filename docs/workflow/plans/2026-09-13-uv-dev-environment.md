# UV Development Environment Plan

Goal: make a fresh checkout reproducibly provision the full development toolchain
with uv.

Architecture: project runtime capabilities remain optional extras; local-only
tools live in uv's `dev` dependency group. `make setup` owns installation and
syncing, and every Makefile gate consumes the resulting locked environment.

Tech stack: Python 3.11+, uv, GNU Make, POSIX shell.

## Global Constraints

- Preserve the existing `full` extra and Python 3.11 support.
- Use the official uv installer only when no `uv` executable is on `PATH`.
- Do not modify a user's shell profile or global PATH.
- Commit uv-managed lock output; never hand-edit it.
- Keep the change limited to development provisioning and its tests/docs.

Expected implementation size: 500–1,000 changed lines (M) — the generated
cross-platform lockfile dominates a small manifest, Makefile, test, and doc change.

## Task 1: Define and lock the development environment

**Files:** `pyproject.toml`, `uv.lock`.

**Interfaces:** consumes the existing `full` extra; provides the default `dev`
group and committed lockfile for Task 2.

**Verification inventory:**

- **Contract:** dependency metadata has a complete reproducible resolution.
  **Mode:** focused-test. **Red:** after adding the `dev` group and before
  regenerating the lockfile, `uv lock --check` fails because the lock is stale.
  **Green:** `uv lock --check` exits 0 after `uv lock`.

Steps:

1. Add pytest, mypy, Ruff, import-linter, build, and twine to `[dependency-groups].dev`.
2. Run `uv lock` with Python 3.11 available and review the generated `uv.lock`.
3. Confirm `uv lock --check` exits 0.

Acceptance: all tools previously installed by the Makefile resolve from project
metadata, and the lockfile is tracked.

## Task 2: Provision and consume the environment

**Files:** `Makefile`, `desloppify/tests/workflows/test_make_setup.py`.

**Interfaces:** consumes `uv.lock` from Task 1; provides `make setup` and
locked Makefile command execution.

**Verification inventory:**

- **Contract:** an available uv executable receives the exact full locked sync
  invocation. **Mode:** focused-test. **Red:**
  `pytest desloppify/tests/workflows/test_make_setup.py -q` fails before the
  test and target exist. **Green:** the same command passes after a fake uv
  executable records `sync --locked --extra full`.

Steps:

1. Write the focused setup-target test using a temporary executable fake uv and
   a command log outside the repository.
2. Run the test to observe the missing-target failure.
3. Add `setup`, a repository-local unmanaged fallback installer, and a locked
   `uv sync --extra full` invocation.
4. Make existing gates invoke `uv run --locked`; retain the old install targets
   as aliases to `setup`.
5. Run the focused test and `make setup`.

Acceptance: the target does not fall back to pip, uses a discovered uv binary
when present, and all gates use the locked project environment.

## Task 3: Document developer use

**Files:** `README.md`.

**Interfaces:** consumes the public `make setup` target; provides developer
instructions.

**Verification inventory:**

- **Contract:** documentation describes the installed command and does not alter
  program behavior. **Mode:** task-test-not-applicable. The README is prose and
  no executable consumer validates its wording; Task 2 directly exercises the
  documented target.

Steps:

1. Add a short development setup section with `make setup` and locked command
   examples.
2. Check the changed documentation for accurate target names.

Acceptance: a developer can set up and use the environment from a fresh checkout.
