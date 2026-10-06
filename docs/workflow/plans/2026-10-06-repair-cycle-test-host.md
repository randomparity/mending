# Plan: verify the repair cycle against a controlled subprocess test host (#32)

Goal: drive `cmd_repair_cycle` through the production `ClaudeHostAdapter` and
`GitHubIssueClient` against a scripted host and a fake `gh`, cover the
11-row matrix of [the spec](../specs/2026-10-06-repair-cycle-test-host-design.md),
fix the expiry-mid-run defect, and publish the enforcement report.

Architecture: one stdlib-only stand-in script plays `claude` and `gh`; one
test module builds a temporary world (bin wrappers, `world.json`, `host.json`,
a git checkout, `state.json`) per test and runs the cycle in process, or in a
child Python process for the crash and overlap rows. One line changes in
`repair_cycle._host_request`.

Tech stack: Python 3.11+, pytest, stdlib `subprocess`/`fcntl`/`json`.

Expected implementation size: 560–700 changed lines (M) — stand-in ~170,
test module ~400–500, production fix ~3, guide section ~40; the denominator
stays the frozen 250 (M).

## Global Constraints

- No new dependencies. The stand-in imports only the stdlib.
- No network: `bin/` first on `PATH`, `GH_CONFIG_DIR` empty, `GH_TOKEN` and
  `GITHUB_TOKEN` unset; `host_executable` is the absolute stand-in path.
- Every wait polls a file with a bounded deadline; no fixed sleeps that must
  be long enough. Every spawned pid is recorded and killed at teardown.
- Do not touch ADRs, `desloppify/engine/_state/persistence.py`, its tests, or
  the `review-prompt-docs` worktree.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## File map

| File | Change | Owns after |
|---|---|---|
| `desloppify/tests/commands/repair_cycle_stand_in.py` | new | scripted `claude` and fake `gh` |
| `desloppify/tests/commands/test_repair_cycle_e2e.py` | new | the matrix, world fixture, `_drive` |
| `desloppify/app/commands/repair_cycle.py` | `_host_request` deadline | expiry bounds the host run |
| `docs/systemd/repair-cycle.md` | new `## Limit enforcement` section | the enforcement report |

Borrowed, confirmed present: `cmd_repair_cycle(args)`;
`repair_cycle.ClaudeHostAdapter`; `host_session_id(attempt_id) -> str`;
`MARKER_VARIABLE`, `ATTEMPT_TAG`; from `test_repair_cycle`: `_approval(**o)`,
`_authority(revision, **o)`, `_config(**o)`, `_state()`, `ITEM`, `LINK`,
`VERSION`; from `test_repair_queue`: `_Source(*manifests)`, `_manifest(blob)`,
`PASS`, `KEY`, `KEY_BODY`, `REPOSITORY`.

## Task 1 — Stand-in, world fixture, and the wiring rows (1, 2, 3)

Interfaces (later tasks rely on):
- `world` fixture → `_World` with `.root: Path`, `.args(**overrides) -> argparse.Namespace`,
  `.host(*steps: dict)`, `.run(**overrides) -> str` (captured stdout),
  `.state() -> dict`, `.launches() -> list[dict]`, `.gh_log() -> list[list[str]]`,
  `.creates(kind) -> int`, `.set_pr(url, state=..., files=...)`, `.close_issue(number)`,
  `.release()` (creates `go`), `.alive_pids() -> list[int]`.
- `_drive(world) -> subprocess.Popen` runs one cycle in a child Python process
  from `world.root/args.json`.

World: `world.json` = `{"issues": [{"number", "url", "state", "title", "body"}],
"prs": [{"number", "url", "state", "body", "files"}], "log": []}`; URLs are
`https://github.com/owner/repository/{issues|pull}/N`. The initial state is
`_state()` with the `github_repair` link pointing at a world issue whose body
holds `KEY_BODY`. The config is `_config(authority=...)` with
`host_executable`, `adept_skills_dir` (manifest `{"name": "adept",
"version": "7.2.0"}` plus one `skills/quest/SKILL.md`), `adept_skills_version`,
and `expires_at` relative to real time.

Verification:
- Discovery-only creates no attempt — Mode: focused-test. `test_discovery_only_is_a_no_op`:
  red until the world serves `issue list --search`; green: `uv run pytest -q
  desloppify/tests/commands/test_repair_cycle_e2e.py -k discovery`.
- Approved repair runs through the production adapter once — Mode: focused-test.
  `test_approved_repair_runs_once_and_settles`: asserts one launch record whose
  argv has `-p`, `--max-budget-usd`, `--session-id host_session_id(attempt)`,
  no `--permission-mode`/`--dangerously-skip-permissions`/`--settings`, the
  `CLAUDE_CONFIG_DIR` the test set, and the PR read through `pr view URL --json
  state,files`. Red before the stand-in creates the PR (`no-pull-request`).
- Missing host/skills parks without a launch — Mode: focused-test.
  `test_missing_host_or_skills_parks`, three parameters.
- Teardown leaves no process — Mode: focused-test. Every test ends with
  `assert world.alive_pids() == []` (inside the fixture, before the kill sweep).

Steps: write the stand-in; write the fixture and `_World`; write rows 1–3;
run the focused command; commit `test(repair-cycle): drive the cycle through a
scripted host`.

## Task 2 — Authority and evidence rows (4, 5) and the expiry fix

Verification:
- Permission denial, revocation, expiry before selection — Mode: focused-test.
  `test_permission_denied_host_fails_without_pr`, `test_revoked_or_expired_attempt_fails_on_resume`
  (params `revoked`, `expired`), `test_expired_approval_never_launches`.
- Expiry bounds the host run — Mending contract changed — Mode: focused-test.
  `test_expiry_mid_run_stops_the_host`: approval `expires_at` = now + 2 s, host
  `hang`; red today (the run lasts until the lease deadline; the test caps the
  wait at 20 s and fails), green after `_host_request` passes
  `deadline=min(lease.deadline, approval.expires_at)`: `runtime-exhausted`,
  pids gone.
- Stale evidence/base — Mode: focused-test. `test_stale_evidence_or_base`
  (params `sync`, `dispatch`, `resume`); the `dispatch` param uses
  `_Source(MANIFEST, MANIFEST, _manifest("e" * 40))` and asserts the source's
  call count so a changed call order fails the test instead of passing vacuously.

Steps: write the tests; observe 4d red; change `_host_request`; green; commit
`fix(repair-cycle): stop the host at its approval's expiry` then
`test(repair-cycle): cover authority and evidence rows`.

## Task 3 — Process rows (6–11)

Verification (all Mode: focused-test, in `test_repair_cycle_e2e.py`):
- `test_overlapping_invocation_exits` — `_drive` holds the host on `wait`;
  in-process run prints `Repair cycle already running.`; state bytes unchanged.
- `test_budget_limits` (params `nested-calls`, `cost-overrun`, `host-budget`,
  `nested-process`) with `call_limit` 5 where calls matter.
- `test_timeout_stops_surviving_child` — runtime 1 minute, clock offset
  −58 s, adapter grace 0.5 s; then a second run launches nothing.
- `test_crash_never_relaunches` (params Mending killed before/after PR, host
  crash before/after PR) — kill the `_drive` child with `SIGKILL` once the
  launch record exists (and, for "after", once the PR exists).
- `test_mismatched_or_unknown_results` (five params; `unknown` patches
  `repair_cycle_host._tree_alive` to `True`).
- `test_restart_after_lease_expiry` (params `open-pr`, `dead-host`) with
  clock offsets past the lease deadline and into the next window.

Steps: write each test, run it, commit `test(repair-cycle): cover process
rows of the host matrix`. Any row whose expected result differs from the
spec is a defect: stop, report it, and fix only with a failing test.

## Task 4 — Enforcement report

Verification: Mode: task-test-not-applicable — prose in the operator guide;
no executable consumer reads it. Each claim cites a matrix test name.

Steps: add `## Limit enforcement` (table: limit, host, Mending, proof,
unsupported/unverified) and the expiry note to the authority paragraph;
commit `docs(repair-cycle): report host and Mending limit enforcement`.
