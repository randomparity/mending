# Verify the repair cycle against a controlled subprocess test host

Issue #32 (part of #19). Governing records: ADR 0010 (host adapter), ADR 0015
(authority), ADR 0016 (composed cycle). No new decision record.

## Problem

`test_repair_cycle.py` injects an in-process `_FakeHost` through `args.host`,
so no test runs the composed `repair-cycle` command through the production
`ClaudeHostAdapter`. `test_repair_cycle_host.py` runs the adapter against a
stand-in executable, but never inside a cycle. Nothing shows the adapter is
wired end to end, that crashes and restarts create no duplicate work, or which
limits the host enforces versus Mending (#19).

## Scope

### Test host

`desloppify/tests/commands/repair_cycle_stand_in.py` is one stdlib-only
script with two roles, chosen by its first argument and installed by the test
as two shell wrappers in a temporary `bin/`:

- **`claude`** (the `host_executable`, absolute path): answers `--version`
  (`2.1.289 (Claude Code)`) and `--help` (lists `--max-budget-usd`,
  `stream-json`, `--forward-subagent-text`). A `-p` run appends one launch
  record (argv, the `MENDING_HOST_SESSION` and `CLAUDE_CONFIG_DIR` values,
  pid) to `launches.jsonl`, reads the attempt ID from the prompt, then runs
  the scenario's steps from `host.json`: `assistant` (N streamed messages,
  optionally with `parent_tool_use_id`), `nested` (a child writing N assistant
  events to its own file, not the stream), `child` (`setsid` and/or ignoring
  `SIGTERM`), `pr`/`issue` (create through `gh` with a tag line naming this or
  another attempt, and the PR's files), `wait` (until a `go` file exists,
  bounded at 30 s), `hang` (ignore `SIGTERM`, sleep), `result` (the
  stream-json result event), `garbage`, and `exit`. Every spawned pid is
  appended to `pids.jsonl`.
- **`gh`**: serves the commands Mending and the host issue (`repo view`,
  `issue list|view|create`, `pr list|view|create`) from `world.json` under an
  `flock`, appending every argv to the world's log. `pr view` returns
  `{"state", "files": [{"path"}]}`, the shape real `gh` 2.97.0 returns.

The test puts `bin/` first on `PATH`, points `GH_CONFIG_DIR` at an empty
directory and clears `GH_TOKEN`/`GITHUB_TOKEN`, so a missed fake can never
reach GitHub. Each test gets its own `tmp_path`; attempt IDs are fresh
`uuid4` values, so the `/proc` marker scan never matches another test's host.
A fixture kills every pid in `pids.jsonl` and `launches.jsonl` at teardown.

### Driving the cycle

`cmd_repair_cycle` runs with a file-backed state (`state.json`), the trusted
`config_data` mapping, no `args.host` (the command constructs
`ClaudeHostAdapter(config)`), no `queue_client` (sync uses the production
`GitHubIssueClient` over the fake `gh`), a real `git` checkout as `repo_root`,
and the injected `source`/`check`/`refresh` seams the existing tests use
(checkout reading, the evidence check, and `scan` have their own tests). The
clock is real time plus a per-test offset, because the adapter compares the
lease deadline with wall time. Two scenarios run the cycle in a child Python
process (`_drive`) so it can be killed or held: crash, and overlap. The
timeout scenarios replace `repair_cycle.ClaudeHostAdapter` with the same class
at a 0.5 s grace period.

### Matrix

Each row is one test (parametrized where a row has variants). "Launches" is
the line count of `launches.jsonl`; every row asserts it and the `gh ... create`
count.

| # | Scenario | Expected |
|---|---|---|
| 1 | no-op/discovery-only: no approval; run twice | `no-op: authority-missing`, no lease, 0 launches, 1 `issue create` total |
| 2 | approved small repair: host opens a tagged PR; PR then merged and repair issue closed; third run | run 1 active (`pull request open`); run 2 settled then no-op; 1 launch, 1 PR |
| 3 | missing host / skills / version mismatch | park `missing-host`/`missing-host-skills`/`host-skills-mismatch`, 0 launches |
| 4a | permission denied: host completes without a PR | `no-pull-request`; next run `disposition-required`; 1 launch |
| 4b | revoked or expired while PR open | resume fails `authority-revoked`/`authority-expired`; later runs never launch; dispose needs `--confirm-stopped` |
| 4c | expired before selection | park `authority-expired`, no lease, 0 launches |
| 4d | approval expires mid-run | host stopped at expiry, `runtime-exhausted` |
| 5 | stale evidence at sync / source changes before dispatch / source changes while PR open | no-op `selected-repair-unavailable` / park `source-not-current` with 0 launches / fail `source-not-current` |
| 6 | overlap: second invocation while the first's host runs | `Repair cycle already running.`, state untouched, 1 launch |
| 7 | nested budget: forwarded subagent messages pass `call_limit`; host-reported cost over the cap; host's own budget stop; nested model process | `budget-exhausted`; `budget-exhausted`; `host-failed`; nested events absent from `consumed_calls`, nested process stopped |
| 8 | timeout with a surviving `setsid` child ignoring `SIGTERM` | `runtime-exhausted`, every recorded pid gone, no relaunch |
| 9 | Mending killed after intent, before / after the host's PR; host crashes before / after its PR | while the host lives: `dispatch-in-flight`; after: `unsettled-reservation` with the PR recorded; host crash: `host-failed`, PR recorded, reported active; 1 launch, ≤1 PR |
| 10 | garbage output; result `is_error` with exit 0; PR tagged for another attempt; PR edits a file outside the approval; worker unverifiable | `host-failed`; `host-failed`; `no-pull-request` with the foreign PR unrecorded; `authority-scope-exceeded`; `dispatch-outcome-unknown`, dispose needs `--confirm-stopped` |
| 11 | restart after lease expiry: PR open past the deadline into the next window; dead host with an intent record | stays active, no new lease until the PR settles; `unsettled-reservation`, then `observation-exhausted`, then after disposal a new attempt with a new attempt ID and session |

The routed checks from #31 are row 2's launch-record assertions: no
permission flag in argv, `CLAUDE_CONFIG_DIR` passed through unchanged, and the
`gh pr view` argv and shape.

### Production defect: authority expiry mid-run (row 4d)

`_authorize` checks `now < expires_at` before dispatch, but the host request's
deadline is the lease deadline, so a dispatch shortly before expiry runs up to
`runtime_minutes` past it. `_host_request` sets
`deadline = min(lease.deadline, approval.expires_at)`; the adapter's existing
timeout stops the tree there (`runtime-exhausted`). Rejected: refusing
dispatch when expiry falls before the lease deadline (parks a repair that
could finish in time); rechecking authority while the host runs (a second
check point for one timestamp).

### Enforcement report

A `## Limit enforcement` section in `docs/systemd/repair-cycle.md`, repeated
in the PR: for USD cost, calls, runtime, permissions, edit scope, and
authority expiry/revocation, which the host enforces, which Mending enforces,
the test row proving it, and what is unsupported or unverified. Unverified
without live credentials (#7): `--max-budget-usd` under the deployment's auth,
managed-settings precedence, the `CLAUDE_CONFIG_DIR`/`ProtectHome` layout, and
systemd stopping an orphaned host when Mending dies (`KillMode` default).

## Failure model

1. **Actors and deployments**
   - Developer workstation and the GitHub Actions Linux runner, `make tests`.
   - The deployment the report describes: the systemd unit and manual pilot (ADR 0006/0010).
2. **Invariants and assets at stake**
   - No test reaches the network or real GitHub.
   - No leaked process after a test; no test signals another test's process.
   - The suite stays deterministic: waits are on files with bounded timeouts, never sleeps tuned to pass.
3. **Accepted failure classes**
   - Real Claude Code stream format or flag drift: the stand-in mirrors 2.1.289 as ADR 0010 records; live drift is #7's pilot.
   - A host orphaned by a Mending crash outside systemd keeps running until it ends; later runs park `dispatch-in-flight` and launch nothing. Reported as a limitation and follow-up candidate, not fixed here.
   - Non-Linux hosts: unsupported (ADR 0010).
4. **Covered elsewhere**
   - `scan` refresh, source checkout reading, and the evidence check: their existing tests.
   - Live credentials, real repairs, and timer activation: #7.

## Success

1. Every matrix row passes against the production adapter and production `gh` client, with the launch and create counts above.
2. A test that leaves a recorded pid alive fails.
3. The enforcement report states, for each listed limit, its enforcer, its proof row, and its gaps.
4. `make lint typecheck arch ci-contracts tests tests-full package-smoke` and the records gate pass.
