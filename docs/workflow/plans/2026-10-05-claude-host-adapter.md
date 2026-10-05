# Implement the Claude Code host adapter

Goal: one production adapter that launches Claude Code with the Adept plugin for a
selected brief and stops or establishes the state of its whole process group.
Architecture: a new module `desloppify/app/commands/repair_cycle_host.py` beside
`repair_cycle.py`; three optional keys on `CycleConfig`. Spec:
`docs/workflow/specs/2026-10-05-claude-host-adapter-design.md`; ADR 0010.
Stack: Python 3.11+, stdlib only (`subprocess`, `os`, `signal`, `shutil`,
`tempfile`, `uuid`, `json`), pytest via `uv`.

## Global Constraints

- No new dependency. No change to `cmd_repair_cycle` behavior, `CycleConfig.park_reason`,
  or persisted state shape. Do not touch `repair_queue.py` files.
- Never pass a permission-bypass flag; argv lists only, no shell.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, `.github/scripts/check-records.sh`.

Expected implementation size: 230–300 changed lines (M) — one ~150-line module,
~15 config lines, ~110 test lines.

## File map

- `desloppify/engine/repair_cycle.py` — owns config decoding; gains three keys.
- `desloppify/app/commands/repair_cycle_host.py` (new) — owns host launch and
  process-group stop/verify.
- `desloppify/tests/commands/test_repair_cycle_host.py` (new) — stand-in host tests.
- `desloppify/tests/commands/test_repair_cycle.py` — config decoding cases.

## Task 1: Config keys

Interfaces: produces `CycleConfig.host_executable: str`,
`CycleConfig.adept_skills_dir: str | None`, `CycleConfig.adept_skills_version: str | None`.

Verification: Mode: focused-test — in `test_repair_cycle.py`, add
`test_config_host_keys_default_and_decode`: a mapping without the keys yields
`"claude"`, None, None; with them yields the stripped values; `{"host_executable": ""}`
raises `ValueError`. Red: `AttributeError` on `host_executable`. Green:
`uv run --locked pytest desloppify/tests/commands/test_repair_cycle.py -q`.

Steps: add the three fields after `cost_cap_usd` with defaults
(`host_executable: str = "claude"`, the other two `None`) so existing
constructor calls stay valid; decode with
`_optional_text(mapping, "host_executable") or "claude"` and `_optional_text` for
the other two. Commit `feat(repair-cycle): add host adapter config keys`.

## Task 2: Adapter

Interfaces: consumes Task 1 fields and `CycleConfig.model`. Produces
`HostRequest`, `HostOutcome`, `ClaudeHostAdapter(config, *, clock=None,
grace_seconds=10.0).run(request) -> HostOutcome`, all in `__all__`.

Verification (all Mode: focused-test in `test_repair_cycle_host.py`; red is
`ModuleNotFoundError` before the module exists; green:
`uv run --locked pytest desloppify/tests/commands/test_repair_cycle_host.py -q`):

- `test_preflight_parks_without_launch` (parametrized): missing model, missing
  executable path, missing skills dir, missing manifest, wrong name, wrong version,
  past deadline → `parked` with the spec reason; the stand-in's marker file is absent.
- `test_completed_run_reports_identity`: stand-in mode `ok` writes argv and stdin to
  a file and prints `{"is_error": false, "session_id": ...}`; outcome `completed`,
  `skills_version == "7.2.0"`, session ID equals the uuid5 derivation, argv contains
  `--plugin-dir`, `--session-id`, `--model`, no `--dangerously-skip-permissions`;
  recorded stdin contains the brief and attempt ID.
- `test_nonzero_and_invalid_output_fail`: mode `fail` (exit 3) → `failed`/`host-error`;
  mode `garbage` → `failed`/`invalid-host-output`.
- `test_timeout_stops_whole_group`: mode `hang` spawns a child that ignores `SIGTERM`,
  writes both PIDs, sleeps; deadline 1.5 s, `grace_seconds=0.5`; outcome
  `stopped`/`timeout`; `os.kill(pid, 0)` raises `ProcessLookupError` for both PIDs.
- `test_exit_with_live_child_stops_child`: mode `orphan` starts a sleeping child and
  exits 0 with valid JSON; outcome `completed` and the child PID is gone.
- `test_unverifiable_group_is_unknown`: mode `hang`; monkeypatch the module's
  `_group_alive` to return True; outcome `unknown`/`timeout-survivors`; then
  `os.killpg` the real group in test cleanup.

The stand-in is a Python script written to `tmp_path/claude` with mode 0o755 and a
`#!{sys.executable}` shebang; tests set `host_executable` to its absolute path and
`FAKE_HOST_MODE` via `monkeypatch.setenv`. The skills fixture is
`tmp_path/adept/.claude-plugin/plugin.json` = `{"name": "adept", "version": "7.2.0"}`.

Steps: write the tests; implement per the spec Behavior section with these private
helpers: `_preflight(config, request, now) -> HostOutcome | None`,
`_manifest_version(skills_dir) -> str | None`, `_group_alive(pgid) -> bool`
(`os.killpg(pgid, 0)`; `ProcessLookupError` → False; `PermissionError` → True),
`_stop_group(proc, grace_seconds) -> bool` (returns True when the group is empty),
`_outcome_from_exit(...)`. Commit `feat(repair-cycle): add Claude Code host adapter`.

## Task 3: ADR 0010

Verification: Mode: task-test-not-applicable — prose record; the records gate checks
its shape (`.github/scripts/check-records.sh`), which is the only executable consumer.
Commit with the design artifacts.
