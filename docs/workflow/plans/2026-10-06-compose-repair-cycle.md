# Implement the composed repair cycle

Goal: make one `repair-cycle` invocation observe recorded work, refresh,
publish, select, authorize, and dispatch one repair through the Claude Code
host, settling attempts by their pull requests within a configured window.
Architecture: the engine owns the window and settlement facts
(`CycleLease.window_start`, `CycleState.settled`); the command composes the
existing seams (`scan` subprocess, `cmd_repair_queue`, `_authorize`,
`_dispatch_host`, `ClaudeHostAdapter`) and drops the receipt protocol.
Spec: `docs/workflow/specs/2026-10-06-compose-repair-cycle-design.md`;
ADR 0016. Stack: Python 3.11+, pytest, mypy, ruff, import-linter.

Expected implementation size: 900–1300 changed lines (L) — engine state
(~120), command composition (~300 changed, ~200 removed), adapter PR lookup
(~50), recipe and guide (~80), tests (~550, mostly rewriting receipt-based
cases onto the fake host).

## Global Constraints

- No new dependency. Decimal for money; timezone-aware datetimes only.
- Park/fail reasons: the ADR 0015 set plus exactly `refresh-failed`,
  `publication-failed`, `window-attempt-complete`, `host-failed`,
  `no-pull-request`, `pull-request-lookup-unavailable`; existing
  `runtime-exhausted`, `budget-exhausted`, `dispatch-outcome-unknown`,
  `observation-exhausted`, `observation-timeout`, `disposition-required`.
  The removed `daily-attempt-complete`, `selection-unavailable`,
  `reconciliation-unavailable`, `invalid-receipt`, `non-usd-receipt`,
  `receipt-correlation-mismatch`, `unknown-receipt`, `merge-permit-exhausted`
  are not emitted.
- Do not touch `desloppify/engine/_state/persistence.py`.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
- Focused command: `uv run --locked pytest desloppify/tests/commands/test_repair_cycle.py desloppify/tests/commands/test_repair_cycle_host.py desloppify/tests/repair_cycle desloppify/tests/ci/test_repair_cycle_recipe.py -q`.

## File map

- `desloppify/engine/repair_cycle.py` — `CycleConfig.window_minutes`,
  `window_start(now, minutes)`, `CycleLease.window_start` (legacy `day_key`
  decode, no `merge_permit`), `CycleState.settled`, `reported_active`
  additions, `begin` window rule; removes `merge_permit_day`,
  `merge_permit_available`, `consume_merge_permit` (spec: State model).
- `desloppify/app/commands/repair_cycle_host.py` — `PullRequest`,
  `ClaudeHostAdapter.pull_requests(urls)` (spec: pull-request check).
- `desloppify/app/commands/repair_cycle.py` — new `cmd_repair_cycle`
  composition, `_observe`, `_check_pull_requests`, `_refresh`, `_publish`,
  `_host_request`; `_dispatch_host` loses its `request` parameter; receipt
  protocol removed (spec: Flow).
- `docs/systemd/mending-repair-cycle.service`, `docs/systemd/repair-cycle.md`.
- Tests: `desloppify/tests/commands/test_repair_cycle.py`,
  `desloppify/tests/commands/test_repair_cycle_host.py`,
  `desloppify/tests/repair_cycle/test_repair_cycle_state.py`,
  `desloppify/tests/ci/test_repair_cycle_recipe.py`.

No caller outside these files imports the removed names (`rg -n
'AdeptReceipt|AdeptCycleClient|consume_merge_permit|merge_permit_available'
desloppify` finds only these modules and their tests).

## Task 1 — Engine: window, settlement, legacy decode

Interfaces produced:
`window_start(now: datetime, minutes: int) -> str`;
`CycleConfig.window_minutes: int = 1440`;
`CycleLease(attempt_id, window_start: str, deadline, call_limit, cost_cap_usd)`;
`CycleLease.create(config, now, *, attempt_id=None)`;
`CycleState.settled -> bool`; `CycleState.reported_active -> bool`;
`CycleState.begin(config, now, *, attempt_id=None) -> CycleLease | None`;
`DispatchRecord.phase` accepts `"settled"`.

Verification:
- Mode: focused-test — window keys: `window_start(datetime(2026,9,13,23,59,tzinfo=UTC), 1440) == "2026-09-13T00:00"`,
  `window_start(..., 360)` floors to `18:00`; red: ImportError.
- Mode: focused-test — legacy lease `{"day_key": "2026-09-13", "merge_permit": true, ...}`
  decodes with `window_start == "2026-09-13T00:00"` and re-encodes without
  `day_key`/`merge_permit`; state with `merge_permit_day` decodes; red: KeyError on `window_start`.
- Mode: focused-test — `settled` truth table: no lease; disposed; legacy terminal
  receipt; dispatch `settled`; no dispatch and no reservation and no active
  receipt → True; dispatch `intent`/`unknown`/returned-with-open-PR, legacy
  active receipt, reserved calls → False.
- Mode: focused-test — `begin` refuses an unsettled attempt, an undisposed
  failure, and the same window; allows the next window after settlement.

Steps: write the tests in `test_repair_cycle_state.py` (replace the
merge-permit test) and the `test_legacy_state_decodes…` case in
`test_repair_cycle.py`; run the focused command, see red; implement:

- `_EPOCH = datetime(2000, 1, 1)`; `window_start` floors
  `(now.replace(tzinfo=None) - _EPOCH)` minutes to a multiple of `minutes`
  and returns `isoformat(timespec="minutes")`.
- `CycleLease.from_mapping`: `window_start` if present, else
  `f"{_parse_day_key(day_key).isoformat()}T00:00"`; validate with
  `datetime.fromisoformat`.
- `_may_replace_lease(window)`: `settled and not awaiting_disposition and
  lease.window_start != window`.
- `reported_active`: legacy active receipt, or dispatch not `settled` and
  (phase is `intent`/`unknown`, or outcome is `unknown`/`completed`, or it
  carries a PR).
- Command side, in the same task so the commit stays green: in
  `desloppify/app/commands/repair_cycle.py` replace `lease.day_key ==
  now.date().isoformat()` with `lease.window_start == window_start(now,
  config.window_minutes)` (reason stays until Task 3), delete the
  `consume_merge_permit` call in `_accept_receipt`, and make
  `_merge_permit_accepts` return `False` (the receipt path is deleted in
  Task 3). Migrate the merge-permit tests in `test_repair_cycle.py`
  (`test_non_usd_receipt_parks_and_consumed_merge_stays_daily`,
  `test_only_an_exact_replayed_merge_receipt_reuses_a_daily_permit`) to assert
  that a `merge_consumed` receipt parks `merge-permit-exhausted` and no
  `merge_permit_day` is written; Task 3 deletes them with the receipt path.

Green: focused command passes. Commit `feat(repair-cycle): key leases to a configured window`.

## Task 2 — Adapter: pull-request lookup

Interfaces produced: `PullRequest(url: str, open: bool, files: frozenset[str])`;
`ClaudeHostAdapter.pull_requests(self, urls: tuple[str, ...]) -> tuple[PullRequest, ...]`
raising `HostLookupError` for a URL not matching
`^https://github\.com/<re.escape(repository)>/pull/[0-9]+$` (compiled with
`re.IGNORECASE`), a failed `gh`,
or JSON without `state` (str) and `files` (list of `{"path": str}`).
`open` is `state == "OPEN"`.

Verification:
- Mode: focused-test — in `test_repair_cycle_host.py`, a fake `gh` on `PATH`
  (the module's existing pattern) printing `{"state":"MERGED","files":[{"path":"a.py"}]}`
  yields `PullRequest(url, False, frozenset({"a.py"}))`; the same URL with
  the owner upper-cased is accepted; a foreign URL raises without running
  `gh`; red: AttributeError.

Steps: test, red, implement with `_output("gh pr view", ["gh", "pr", "view", url, "--json", "state,files"])`,
green, commit `feat(repair-cycle): read pull-request state and files`.

## Task 3 — Command composition

Interfaces consumed: Task 1 and Task 2 names; existing `_authorize`,
`_work_item`, `_trusted_authority`, `_replay_dispatch`, `_add_references`,
`_recheck_active`, `source_comparison`, `cmd_repair_queue`, `build_brief`,
`render_brief`. Interfaces produced:
`_dispatch_host(args, state, cycle_state, config, adapter) -> HostOutcome | None`;
`_replay_dispatch(state, cycle_state, adapter, repo_root: Path) -> None` and
`_add_references(state, cycle_state, adapter, repo_root: Path) -> bool`
(the `HostRequest` parameter is replaced by the fallback repo root, the only
field they used); `_cycle_lock(args) -> ContextManager[bool]` (True when held
or when `state_data` is injected);
test injection attributes `args.host`, `args.refresh` (callable returning a
park reason or None), `args.queue_client`, `args.repo_root`.

Verification (each focused-test, `test_repair_cycle.py`, red before the change
because `args.host` is ignored and the stub raises):
- spec Success 1–6, 8, 9, each as one test using `_FakeHost` extended with
  `prs: tuple[PullRequest, ...] | HostLookupError` and a recording refresh
  and queue client;
- Success 4's cycle-lock case: hold `fcntl.flock` on `<tmp>/state.json.cycle.lock`
  in the test, run the command with `state=<tmp>/state.json`, assert the
  refresh callable was not called and the file bytes are unchanged;
- Success 8's file-backed case: `state_data=None`, `state=<tmp>/state.json`
  seeded with `_revalidated_state()`; the injected refresh adds nothing, the
  injected `queue_client` returns the linked issue so sync writes the
  `github_repair` link; assert the saved file has the link and a lease bound
  to `ITEM`; a refresh returning `"refresh-failed"` leaves that
  `parked_reason` in the file;
- the production refresh argv: monkeypatch `repair_cycle.subprocess.run` to
  record `(argv, cwd)` and return `CompletedProcess(argv, 0)`; assert argv
  `[sys.executable, "-m", "desloppify", "scan", "--state", str(abs_state)]`;
- the existing `_dispatch_host` budget, intent, replay, and authority tests
  migrated to the new signature (the request now comes from the binding;
  assert `host.requests[0].authorized_scope` names `src/impl.py`).

Steps:
0. `_cycle_lock(args)`: when `state_data` is a dict yield True; else open
   `state_path(args).with_suffix(".json.cycle.lock")` (`O_CREAT|O_RDWR`),
   `fcntl.flock(fd, LOCK_EX | LOCK_NB)`; on `BlockingIOError` yield False;
   always close the fd. `cmd_repair_cycle` prints `Repair cycle already
   running.` and returns when it yields False.
1. Rewrite `cmd_repair_cycle` as the spec's Flow; `_new_work_refusal(cs, config, now)`
   returns the gate reason; `_no_op(state, cs, reason)` clears `parked_reason`
   and prints `Repair cycle no-op: {reason}.`
2. `_refresh(args, config) -> str | None`: `args.refresh()` when supplied;
   otherwise `subprocess.run([sys.executable, "-m", "desloppify", "scan", "--state",
   str(state_path(args).resolve())], cwd=repo_root, timeout=config.runtime_seconds,
   check=False)` (`# nosec B603`), omitting the two `--state` tokens only when
   `state_path(args)` is None; nonzero, `OSError`, or `TimeoutExpired` →
   `"refresh-failed"`.
3. `_publish(args, config) -> str | None`: `cmd_repair_queue(argparse.Namespace(
   repair_queue_action="sync", repository=config.repository, apply=True,
   state=getattr(args, "state", None), state_data=getattr(args, "state_data", None),
   source=getattr(args, "source", None), check=getattr(args, "check", None),
   client=getattr(args, "queue_client", None), source_root=None, revision="HEAD"))`
   — in production `state_data`, `source`, `check`, and `client` are None, so
   sync uses the file, the project root at HEAD, the real check, and
   `GitHubIssueClient()`; `CommandError`, `RuntimeError`, `OSError`,
   `ValueError` → `"publication-failed"`. A step 4/5 reason is recorded by
   re-entering `_locked_state(args)`, decoding, `_park`, and saving.
4. `_observe(args, state, cs, config, host)`: allowance, `_call_within(observation_seconds, …)`,
   then the spec's Observation cases; after `_replay_dispatch` with the worker
   not alive and `dispatch.pull_requests` non-empty, run the scope part of
   `_check_pull_requests`; a successful PR read sets `observation_calls = 0`.
5. `_check_pull_requests(args, state, cs, config, host)`: the spec's ordered
   checks; approved files from `_trusted_authority` and `cs.authority["key"]`.
6. `_dispatch_host`: build `HostRequest` via `_host_request(state, cs, binding, lease, repo_root, approved_files)`
   after `_authorize` (`authorized_scope` = `"repair: " + ", ".join(sorted(approved_files))`);
   after the run, fail per outcome; for `completed`, when `_add_references`
   returned False park `pull-request-lookup-unavailable`, else
   `_check_pull_requests`.
7. Delete the receipt protocol names listed in the spec.

Green: focused command. Commit `feat(repair-cycle): compose the one-shot cycle through the host`.

## Task 4 — Recipe and guide

Verification:
- Mode: focused-test — `test_repair_cycle_recipe.py` asserts
  `Environment=CLAUDE_CONFIG_DIR=/var/lib/mending/claude`, that
  `ReadWritePaths` contains `/var/lib/mending/claude` and
  `/var/lib/mending/repository-worktrees`, `ProtectHome=true`, the
  unchanged `ExecStart`, and that the guide contains `"host_executable": "/`
  and `window_minutes`; it drops the `parks before selection` assertion;
  red: KeyError on `Environment`.
- Mode: task-test-not-applicable — the guide's prose (observation and its
  reset, settlement and closed-unmerged retries, approval `files` as the whole
  edit set including tests, `expires_at` past review, the operator keeping the
  checkout current, no manual `scan`/`repair-queue` on the service state while
  the unit runs) has no executable consumer.

Steps: edit the service and guide; green; commit `docs(repair-cycle): wire the recipe to the composed path`.

## Final

Run every guardrail bare; record results in the forge ledger.
