# Compose the one-shot repair cycle through the host adapter

Issue #31 (part of #19). Decision record: [ADR 0016](../../adr/0016-composed-repair-cycle.md).
Builds on ADRs 0007, 0010, 0014, and 0015.

## Problem

`cmd_repair_cycle` still begins a day-keyed lease and asks an
`AdeptCycleClient` to `select` and `reconcile`; the production default raises.
The sibling seams exist but nothing calls them in order: `_dispatch_host`
(budget admission, dispatch intent, host run, #29/#30), `_authorize` (#26),
repair-queue `sync` (revalidation and publication, #18/#22), and the
`ClaudeHostAdapter` (#27). The lease carries a merge permit that receipts can
consume, and the receipt path's `_limit_failure` checks usage after spending.

## Scope

### Flow of one invocation

1. **Dispose.** `--dispose-attempt` is unchanged and exits.
2. **Observe** (under the state lock). When the current attempt is not
   *settled* (below), observe it through the host adapter and stop: no new
   selection while work is recorded active.
3. **Gate** (under the lock). Park on `config.park_reason`,
   `disposition-required` (failed, undisposed attempt), or
   `window-attempt-complete` (the current lease is in this window).
4. **Refresh** (lock released). Run `scan` as a subprocess:
   `sys.executable -m desloppify scan [--state STATE]`, working directory the
   project root, bounded by the configured runtime. Nonzero exit, timeout, or
   launch failure parks `refresh-failed`.
5. **Revalidate and publish** (lock released). Call `cmd_repair_queue` in
   process with `sync --apply --repo REPOSITORY` and the cycle's `--state`. An
   exception parks `publication-failed`. Sync's own rechecks and its at-most-one
   publication (ADR 0014) are unchanged.
6. **Select and bind** (re-take the lock, re-run step 3's gate).
   `_authorize(select=True)`. `selected-repair-unavailable` and
   `authority-missing` end the run as a valid **no-op**
   (`Repair cycle no-op: <reason>.`, `parked_reason` cleared, no lease). Any
   other refusal parks as today. On success `begin` creates the lease and the
   bound authority is persisted.
7. **Dispatch.** `_dispatch_host(args, state, cycle_state, config, adapter)`:
   its `_authorize` call is the execution-time source and authority recheck
   (operator scope addition); the `HostRequest` is built from the binding that
   call returns and the selected work item (rendered brief, manifest revision,
   `files`, deadline). No second recheck is added.
8. **Settle the outcome** (same run). `failed` fails `host-failed`; `stopped`
   fails `runtime-exhausted` (timeout) or `budget-exhausted` (call limit);
   `unknown` fails `dispatch-outcome-unknown` and stays reported active;
   `completed` runs the pull-request check below.

`_select`, `_reconcile`, `_accept_receipt`, `_receipt_reason`,
`_limit_failure`, `_merge_permit_accepts`, `_has_terminal_receipt`,
`AdeptReceipt`, `AdeptCycleClient`, and `_UnavailableAdeptCycleClient` are
removed. `args.host` (an adapter-shaped object) replaces `args.client` for
tests; production constructs `ClaudeHostAdapter(config)`.

### Observation (step 2)

Each observing run spends one call of the existing observation allowance
(#28): at the limit it fails `observation-exhausted`; the observation is
bounded by `observation_seconds` and a timeout parks `observation-timeout`.

- Dispatch at `intent` or `unknown`, or returned with an `unknown` outcome:
  the existing `_replay_dispatch` (never relaunches).
- No dispatch record but a legacy receipt reported `active`: fail
  `dispatch-outcome-unknown` (no host can observe it; disposal needs
  `--confirm-stopped`).
- Returned dispatch: add references, then the pull-request check.

**Pull-request check.** The adapter's new `pull_requests(urls)` reads each
recorded PR with `gh pr view URL --json state,files`. A URL that is not
`https://github.com/<repository>/pull/<number>` or a failed read parks
`pull-request-lookup-unavailable`. Then, in order:

- a PR touching a file outside the bound approval's `files` fails
  `authority-scope-exceeded` (#26's actual-edits check; an approval that no
  longer decodes or exists falls through to the resume recheck);
- a `completed` dispatch with no PR fails `no-pull-request`;
- any open PR: the attempt stays active (`Repair cycle active: pull request
  open.`) and the existing resume recheck (`_recheck_active`) runs;
- otherwise every PR is merged or closed: the dispatch phase becomes
  `settled` and the attempt is settled.

### State model

- **Settled** (`CycleState.settled`): no lease; or disposed; or a legacy
  `terminal` receipt; or dispatch phase `settled`; or no dispatch record, no
  reservation, and no `active` legacy receipt (nothing was launched).
- **Reported active** gains: a returned dispatch that is `completed` or
  carries a PR, and is not `settled`. Disposal of such an attempt needs
  `--confirm-stopped`.
- **Window.** `CycleConfig.window_minutes` (positive, default 1440). A
  window's key is its start, `YYYY-MM-DDTHH:MM`, from host-local wall time
  floored to `window_minutes` since 2000-01-01T00:00. With the default, every
  window is a host-local day. `CycleLease.window_start` replaces `day_key`;
  `begin` refuses while the attempt is unsettled, awaiting disposition, or in
  the same window.
- **Legacy decode.** A lease with `day_key` (no `window_start`) decodes with
  `window_start = <day_key>T00:00`; `merge_permit` is ignored. A state
  `merge_permit_day` and an `authoritative_receipt` with `merge_consumed` are
  ignored except for the receipt `state` used above. New writes carry neither
  field. Attempt history entries decode either form.

### Systemd recipe

The service keeps `ExecStart=… repair-cycle --config … --state …`; refresh
and sync inherit that `--state`, so they share the cycle's file. It gains
`Environment=CLAUDE_CONFIG_DIR=/var/lib/mending/claude` and that directory in
`ReadWritePaths` (Claude Code reads credentials and settings there and writes
session files), keeping `ProtectHome=true`. The guide's configuration example
gains an absolute `host_executable` outside `/home` and the Adept skills
directory and version, and its observation, window, and adapter text is
updated to this flow. Prerequisite wording stays with #16.

## Failure model

1. **Actors and deployments**
   - Local operator running `repair-cycle` manually (pilot, #7).
   - The systemd timer as the `mending` account on one Linux host.
   - The Claude Code host and its model, running as the same account.
2. **Invariants and assets at stake**
   - At most one active attempt and one new lease per window across restarts.
   - No relaunch of a recorded dispatch; no merge by Mending.
   - An open repair PR or an unverified worker keeps new work blocked.
   - Existing state files decode.
3. **Accepted failure classes**
   - The host can rewrite the state file (dispatch phase, PR list) and so
     unblock new work; accepted as in ADR 0015, which places the same account
     boundary on authority.
   - A PR open longer than `observation_call_limit` runs fails
     `observation-exhausted`; the operator disposes after it closes (bounded,
     fail-closed).
   - A refused dispatch after `begin` spends the window; conservative.
   - Changing `window_minutes` re-aligns windows once; operator-controlled.
4. **Covered elsewhere**
   - Commit-bound tests, review evidence, and draft-only PRs inside the run:
     the Adept skills the host runs (ADR 0007), prompt from #27.
   - `--max-budget-usd` binding under deployment auth, scripted-host matrix:
     #32. Live activation and credentials: #7. Wording: #16.

## Threat model

- **Boundaries.** Adds: PR URLs from the host-writable state file passed to
  `gh pr view`; PR file lists compared with the approval. Widens: the cycle now
  runs `scan` and GitHub publication itself.
- **Actors.** The coding host's model (same account; untrusted for authority);
  GitHub content authors. Trust sits in the root-owned config (ADR 0015).
- **Controls.** URLs must match the configured repository's PR URL pattern
  before use, argv only, no shell. PR files are checked against the trusted
  approval, not the state copy. Subprocesses use fixed argv and bounded time.
- **Out of scope.** A same-account host tampering with state (accepted
  above); a compromised `gh` or `claude` binary.

## Success

Each item names its test; all are in `desloppify/tests/commands/test_repair_cycle.py`
unless noted.

1. A fresh run with a published, approved repair calls refresh, sync, and
   `host.run` once each in that order and records a dispatch.
2. No published repair, or no approval: no lease, `host.run` not called,
   output `Repair cycle no-op`.
3. A recorded active attempt (open PR, live worker, intent, unknown) is
   observed and refresh, sync, and `run` are not called.
4. A second run in the same window parks `window-attempt-complete`; a run in
   the next window after the PR closed dispatches again; `window_minutes` keys.
5. Open PR keeps the attempt active across runs; a closed PR settles it.
6. PR file outside approval fails `authority-scope-exceeded`; completed with
   no PR fails `no-pull-request`; `failed`/`stopped`/`unknown` outcomes fail
   with their reasons and block new work until disposed.
7. Legacy state (`day_key`, `merge_permit`, `merge_permit_day`,
   `merge_consumed` receipts) decodes; nothing consumes or checks a permit
   (`desloppify/tests/repair_cycle/test_repair_cycle_state.py`).
8. Refresh or sync failure parks before selection.
9. `_dispatch_host` builds the request from the dispatch-time binding: a brief
   changed between selection and dispatch parks without `run`.
10. The adapter's `pull_requests` rejects a foreign URL and parses state and
    files (`desloppify/tests/commands/test_repair_cycle_host.py`).
11. The recipe test asserts `CLAUDE_CONFIG_DIR`, `ProtectHome=true`, the
    unchanged `ExecStart`, and the guide's absolute `host_executable`
    (`desloppify/tests/ci/test_repair_cycle_recipe.py`).
