# Plan: verify the repair cycle against a controlled subprocess test host (#32)

Goal: drive `cmd_repair_cycle` through the production `ClaudeHostAdapter` and
`GitHubIssueClient` against a scripted host and a fake `gh`, cover the
11-row matrix of [the spec](../specs/2026-10-06-repair-cycle-test-host-design.md),
fix the expiry-mid-run defect, and publish the enforcement report.
Review dispositions (design pass 1 failure-injection, pass 2
simplification-first) are folded into the spec's matrix and the tasks below.

Architecture: one stdlib-only stand-in script plays `claude` and `gh`; one
test module builds a temporary world (bin wrappers, `world.json`, `host.json`,
a git checkout, `state.json`) per test and runs the cycle in process, or in a
child Python process for the crash and overlap rows. `_host_request` caps the
deadline at the approval's expiry, and `_dispatch_host`/`_outcome_failure`
report that stop as `authority-expired`.

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
| `desloppify/app/commands/repair_cycle.py` | `_host_request` deadline; `_dispatch_host` and `_outcome_failure` reason | expiry bounds and names the host stop |
| `docs/systemd/repair-cycle.md` | new `## Limit enforcement` section | the enforcement report |

Borrowed, confirmed present: `cmd_repair_cycle(args)`;
`repair_cycle.ClaudeHostAdapter`; `host_session_id(attempt_id) -> str`;
`MARKER_VARIABLE`, `ATTEMPT_TAG`; from `test_repair_cycle`: `_approval(**o)`,
`_authority(revision, **o)`, `_config(**o)`, `_state()`, `ITEM`, `LINK`,
`VERSION`; from `test_repair_queue`: `_Source(*manifests)` (with `.calls`),
`_manifest(blob)`, `MANIFEST`, `PASS`, `REPOSITORY`, `_revalidated_state()`;
`CheckResult(outcome, reason, anchors)` from `desloppify.engine.repair_check`;
`repair_cycle_host._tree_alive(pgid, marker)` and the module-level `datetime`
name used by `run` and `_watch`.

## Task 1 — Stand-in, world fixture, and the wiring rows (1, 2, 3)

Interfaces (later tasks rely on):
- `world` fixture → `_World` with `.root: Path`, `.args(**overrides) -> argparse.Namespace`,
  `.host(*steps: dict)`, `.run(**overrides) -> str` (captured stdout),
  `.state() -> dict`, `.launches() -> list[dict]`, `.gh_log() -> list[list[str]]`,
  `.creates(kind) -> int`, `.set_pr(url, state=..., files=...)`, `.close_issue(number)`,
  `.release()` (creates `go`), `.alive_pids() -> list[int]`.
- `world.drive(hold=0) -> subprocess.Popen` runs one cycle in a child Python
  process. `drive.json` carries only data (config, clock offset, `hold`); the
  child's `_drive()` rebuilds the seams from this module (`_Source` or
  `_HeldSource(root, hold)`, `PASS`, a no-op `refresh`, the same clock).
- `world.now(tz)` is the one test clock; `world.use_clock(patch)` installs it as
  `repair_cycle_host.datetime`; `world.processes()` lists live non-zombie pids
  whose environ carries `STAND_IN_WORLD=<root>`.

World: `world.json` = `{"repository", "issues": [{"number", "url", "state",
"title", "body"}], "prs": [{... "files"}], "log": []}`, initially empty; URLs are
`https://github.com/owner/repository/{issues|pull}/N`. The initial state is the
unpublished `_revalidated_state()`, so the cycle's sync creates the issue. The config is `_config(authority=...)` with
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
- Teardown leaves no process — Mode: focused-test. The `world` fixture kills
  and fails on any `world.processes()` after the test (seen red while writing
  the crash rows, when a held host outlived its test).

Steps: write the stand-in; write the fixture and `_World`; write rows 1–3;
run the focused command; commit `test(repair-cycle): drive the cycle through a
scripted host`.

## Task 2 — Authority and evidence rows (4, 5) and the expiry fix

Verification:
- Permission denial, revocation, expiry before selection — Mode: focused-test.
  `test_permission_denied_host_fails_without_pr`, `test_revoked_or_expired_attempt_fails_on_resume`
  (params `revoked`, `expired`), `test_expired_approval_never_launches`.
- Expiry bounds the host run — Mending contract changed — Mode: focused-test.
  `test_approval_expiry_stops_a_running_host`: approval expires 30 min into a
  90-minute lease, host `child` + `hang`, `late_clock` jumps one hour once the
  child exists. Red today: the lease deadline has not passed, `hang` ends after
  its 30 s bound, and the run reports `host-failed`. Green after the deadline
  cap and the reason mapping: `authority-expired`, outcome `stopped`, no
  process left.
- Stale evidence/base — Mode: focused-test. `test_stale_evidence_or_base`
  (three tests); the dispatch-recheck test uses `_Source` with four current
  manifests then a stale one and asserts `calls == 5` and that a lease exists,
  so a changed call order fails instead of passing vacuously.

Steps: write the tests; observe 4d red; change `_host_request`; green; commit
`fix(repair-cycle): stop the host at its approval's expiry` then
`test(repair-cycle): cover authority and evidence rows`.

## Task 3 — Process rows (6–11)

Verification (all Mode: focused-test, in `test_repair_cycle_e2e.py`):
- `test_overlapping_invocation_exits` — `_drive` holds the host on `wait`;
  in-process run prints `Repair cycle already running.`; state bytes unchanged.
- `test_budget_limits` (params `nested-calls`, `cost-overrun`, `host-budget`,
  `nested-process`) with `call_limit` 5 where calls matter.
- `test_timeout_stops_a_surviving_child_and_never_relaunches` — runtime 30
  minutes, `late_clock`, grace 0.5 s; the next window parks
  `disposition-required`.
- Crash: `test_a_cycle_killed_before_dispatch_launches_nothing` (`hold=5`
  blocks the dispatch-recheck read), `test_a_cycle_killed_after_intent_before_launch_never_launches`
  (a `hold-probe` file blocks `--version`), `test_a_cycle_killed_during_its_host_never_relaunches_it`
  (before/after PR; continues two days later through `observation-exhausted`,
  `--confirm-stopped` disposal, and a second attempt — row 11's dead-host arm),
  `test_a_crashed_host_fails_the_attempt` (before/after PR). Each kills the
  `_drive` child with `SIGKILL` at a file-signalled point.
- `test_mismatched_or_unknown_results` (five params; `unknown` patches
  `repair_cycle_host._tree_alive` to `True`).
- `test_an_open_pull_request_holds_the_attempt_past_its_lease` — two days
  later, then merged.

Steps: write each test, run it, commit `test(repair-cycle): cover process
rows of the host matrix`. A row whose result differs from the spec is first
checked against ADRs 0010/0015/0016 and the code; only a contradiction of a
completion criterion is a defect, reported and fixed with a failing test.

## Task 4 — Enforcement report

Verification: Mode: task-test-not-applicable — prose in the operator guide;
no executable consumer reads it. Each claim cites a matrix test name.

Steps: add `## Limit enforcement` (table: limit, host, Mending, proof,
unsupported/unverified, including the orphaned-host row) and the expiry note
to the authority paragraph;
commit `docs(repair-cycle): report host and Mending limit enforcement`.
