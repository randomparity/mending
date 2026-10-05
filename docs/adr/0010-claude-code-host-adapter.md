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
- The host runs in a new session (`start_new_session=True`) with a marker
  environment variable, `MENDING_HOST_SESSION=<session ID>`. The worker tree is
  the host's process group plus every same-user process whose environment
  carries the marker. Claude Code runs its tool commands in their own sessions
  (observed: the Bash tool shell's SID differs from the `claude` process's SID,
  Claude Code 2.1.289, Linux), so the group alone is not the tree.
- Timeout or cancellation (`SIGINT`, `SIGTERM`, `SIGHUP` while the host runs on
  the main thread) sends `SIGTERM`, then `SIGKILL`, to the group and to every
  marked process, reaps the leader, and checks again. An empty tree is
  `stopped`; any remaining member, or a missing `/proc`, is `unknown`. The same
  check runs after a normal exit, so a finished wrapper is not taken as proof
  that its workers stopped.
- The session ID is derived from the durable attempt ID, so the host session
  correlates with Mending's attempt record without a second claim system. It is
  single-use: rerunning an attempt needs a new attempt ID (#30, #31).

## Consequences

- Budget admission (#29), dispatch records (#30), and wiring into
  `repair-cycle` (#31) extend this adapter; it enforces only the deadline.
- The capability evidence is the CLIs' documented options plus the observed
  session layout; no paid host call was made (live runs belong to #7).
  `--max-budget-usd` is documented "only works with --print" for API calls;
  whether it binds under the deployment's auth mode is reported by the
  host-versus-Mending limit matrix (#32). If it does not, the choice rests on
  permission and cancellation fit alone, and this record is revisited.
- A descendant that both leaves the group and clears or replaces its
  environment, or changes user, is not observed.
- The reported Adept version is the configured plugin directory's. The host
  account must not enable another `adept` plugin, whose skills could shadow it.
- The systemd unit hides home directories (`ProtectHome=true`). Running this
  host there needs an absolute `host_executable` and a readable Claude config
  directory with the account's credentials and permission rules exposed to the
  unit; that wiring belongs to #31 and the live pilot to #7.
- Linux is the only supported repair host.

## Considered & rejected

- **Do nothing.** verified: `_UnavailableAdeptCycleClient` in
  `repair_cycle.py` at `67889ca` raises for every call; issue #27 asks for a
  production host.
- **Reuse `codex_batch.py`.** verified: it retries timed-out attempts
  (`run_codex_batch` at `67889ca`), which would launch a second worker, and its
  termination path (`review/runner_process_impl/io.py` `_terminate_process`)
  signals only the direct child.
- **Use the Codex CLI as host.** verified: `codex exec --help` (codex-cli
  0.160.0) offers `--sandbox` for permissions and no spending-limit option,
  while `claude --help` lists `--max-budget-usd`. Cancellation is a process
  property either host needs from this adapter, so usage limits decide.
- **Keep the in-process `SIGALRM` deadline.** verified: `_call_before_deadline`
  in `repair_cycle.py` at `67889ca` returns without a deadline off the main
  thread and has no subprocess handling.
- **Contain the tree with a cgroup or child subreaper.** judgment: complexity;
  a cgroup needs delegated privileges, and a subreaper makes Mending reap every
  orphan, competing with other `subprocess` users for exit statuses.
- **Pass `--dangerously-skip-permissions`.** judgment: fit; it moves the
  permission boundary from operator configuration into code.
- **Support both hosts.** judgment: complexity; ADR 0007 rejects a registry.
