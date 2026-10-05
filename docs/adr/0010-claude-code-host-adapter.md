# 0010 — Run repairs through a Claude Code host with process-group cancellation

## Status

Accepted (2026-10-05)

## Context

ADR 0007 makes Mending own one concrete host adapter that launches a coding
host running the installed Adept skills. `repair-cycle` has only an unavailable
stub, and its one deadline control is a `SIGALRM` around an in-process call,
which cannot stop or observe a worker subprocess tree. Issue #27 requires the
host to be chosen from demonstrated permission, cancellation, and usage-limit
capabilities, after evaluating reuse of the Codex batch runner.

## Decision

- The host is the Claude Code CLI (`claude -p`), launched by one adapter class,
  `ClaudeHostAdapter`, in `desloppify/app/commands/repair_cycle_host.py`. There
  is no registry and no generic host protocol.
- Capability evidence (checked 2026-10-05 against `claude --help`, Claude Code
  2.1.289): non-interactive print mode with `--output-format json`;
  `--plugin-dir` loads the Adept plugin for one session; `--session-id` binds a
  caller-chosen UUID; `--model`; permission control through `--permission-mode`
  and settings; `--max-budget-usd` for a spending limit.
- Permissions come from the host account's own Claude settings (ADR 0006's
  dedicated account). The adapter never passes a permission-bypass flag.
- The Adept version identity is the `version` in the plugin directory's
  `.claude-plugin/plugin.json`; it must equal the configured version or the
  run parks.
- The host runs in a new session (`start_new_session=True`), so its process
  group ID equals its PID. Timeout or cancellation sends `SIGTERM`, then
  `SIGKILL`, to the whole group, reaps the leader, and checks the group with
  signal 0. An empty group is `stopped`; a group that still has members after
  the grace period is `unknown`. The same check runs after a normal exit, so
  a finished wrapper is not taken as proof that its workers stopped.
- The session ID is derived from the durable attempt ID, so the host session
  correlates with Mending's attempt record without a second claim system.

## Consequences

- Budget admission (#29), dispatch records (#30), and wiring into
  `repair-cycle` (#31) extend this adapter; it enforces only the deadline.
- Verification covers the process group. A descendant that calls `setsid`
  leaves the group and is not observed. On the timer path the systemd unit's
  default control-group kill stops such a process when the service exits.
- Non-Linux POSIX hosts work; Windows has no process groups and is not a
  supported repair host.

## Considered & rejected

- **Reuse `codex_batch.py` with the Codex CLI.** verified: `codex exec --help`
  (codex-cli 0.160.0) lists no spending-limit option, while `claude --help`
  lists `--max-budget-usd`. The runner also retries timed-out attempts, which
  would launch a second worker, and its termination path
  (`review/runner_process_impl/io.py` `_terminate_process` at `67889ca`)
  signals only the direct child.
- **Keep the in-process `SIGALRM` deadline.** verified: `_call_before_deadline`
  in `repair_cycle.py` at `67889ca` returns without a deadline off the main
  thread and has no subprocess handling.
- **Contain the tree with a cgroup or child subreaper.** judgment: complexity;
  it needs privileges or `prctl` via ctypes for a case the systemd unit already
  covers.
- **Pass `--dangerously-skip-permissions`.** judgment: fit; it moves the
  permission boundary from operator configuration into code.
- **Support both hosts.** judgment: complexity; ADR 0007 rejects a registry.
