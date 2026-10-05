# Claude Code host adapter design

Issue #27, part of #19. Decision record: [ADR 0010](../../adr/0010-claude-code-host-adapter.md).

## Problem

`repair-cycle` has no production host, and its only deadline control cannot stop
or observe a worker process tree.

## Scope

One adapter, `ClaudeHostAdapter`, in a new module
`desloppify/app/commands/repair_cycle_host.py`, plus three optional keys in the
existing repair-cycle JSON config. Not wired into `cmd_repair_cycle` (#31); no
budget admission (#29); no dispatch record (#30). No ownership transition: a clean
extension beside the existing `repair_cycle.py`, whose `_UnavailableAdeptCycleClient`
stays the command default until #31.

### Config (`CycleConfig`, `desloppify/engine/repair_cycle.py`)

- `host_executable`: optional non-empty string, default `"claude"`.
- `adept_skills_dir`: optional non-empty string, default absent.
- `adept_skills_version`: optional non-empty string, default absent.
- `model` is the existing key. `park_reason` is unchanged, so existing configs and
  state decode and behave as before.

### Interface

- `HostRequest(attempt_id: str, brief: str, source_revision: str,
  authorized_scope: str, repo_root: Path, deadline: datetime)`; `deadline` is
  timezone-aware.
- `HostOutcome(state, reason, session_id, skills_version, exit_code, result, detail)`;
  `state` is one of `parked`, `completed`, `failed`, `stopped`, `unknown`; `detail`
  is the last 2,000 characters of host stderr for `failed`, else None.
- `ClaudeHostAdapter(config, *, grace_seconds=10.0)`; `run(request) -> HostOutcome`.
  The current time is `datetime.now(request.deadline.tzinfo)`.

### Behavior

1. Preflight, in order, each returning `parked` with no process launched:
   `missing-model`; `missing-host` when `shutil.which(host_executable)` is None;
   `missing-host-skills` when the skills dir, version key, or manifest
   `<dir>/.claude-plugin/plugin.json` is absent or unreadable;
   `host-skills-mismatch` when the manifest `name` is not `adept` or its `version`
   differs from `adept_skills_version`; `runtime-exhausted` when the deadline has
   passed; `host-launch-failed` when `Popen` raises `OSError`.
2. Launch `[exe, "-p", "--output-format", "json", "--model", model,
   "--session-id", session_id, "--plugin-dir", skills_dir]` with `cwd=repo_root`,
   `start_new_session=True`, the inherited environment plus
   `MENDING_HOST_SESSION=<session_id>`, the prompt on stdin from a temp file, and
   stdout and stderr to temp files removed after reading. `session_id` is
   `uuid5(NAMESPACE_URL, "mending-attempt:" + attempt_id)` and is single-use; this
   adapter does not support rerunning an attempt ID. The prompt states the attempt
   ID, source revision, authorized scope, and brief, and instructs the host to use
   the installed Adept skills and stop at a draft pull request without merging.
3. The worker tree is the host's process group plus every process whose
   `/proc/<pid>/environ` contains the marker entry. Stopping the tree: up to two
   rounds (`SIGTERM`, then `SIGKILL`) sent to the group and to each marked PID, each
   followed by polling for up to `grace_seconds` until the leader is reaped, the
   group is gone (`os.killpg(pgid, 0)` raises `ProcessLookupError`), and no marked
   process remains. Unreadable `environ` entries of other PIDs are skipped; a
   missing `/proc` means the tree cannot be verified empty.
4. Wait until the deadline. On timeout, stop the tree: empty → `stopped`/`timeout`;
   otherwise `unknown`/`timeout-survivors`.
5. Cancellation: while the host runs on the main thread, `SIGTERM` and `SIGHUP`
   handlers raise `SystemExit(128 + signum)`; the handlers and `SIGINT`'s are
   restored afterwards. On any `BaseException` after launch (including
   `KeyboardInterrupt`), ignore further `SIGINT`/`SIGTERM`/`SIGHUP`, stop the tree,
   and re-raise. The persisted lease has no terminal receipt, so the next run treats
   the attempt as active. Off the main thread no handlers are installed.
6. After a normal exit, stop any remaining tree members; survivors →
   `unknown`/`worker-survivors`. Otherwise exit 0 with a JSON object whose
   `is_error` is false → `completed`; unparseable output → `failed`/
   `invalid-host-output`; anything else → `failed`/`host-error`. `result` is the
   parsed object or None.

An `unknown` outcome keeps the active attempt: `CycleState.begin` replaces a lease
only after a terminal receipt (existing rule), and #31 maps `unknown` to that path.

## Failure model

1. Actors and deployments: a local operator running a one-shot command; the
   systemd service from ADR 0006 under a dedicated account, once #31/#7 expose a
   Claude config directory and absolute executable to the unit (ADR 0010). Linux only.
2. Invariants and assets: after `run` returns `stopped`, `completed`, or `failed`,
   no member of the worker tree (step 3) is running; no launch when host, skills,
   or model are missing; no permission bypass; existing config and state decode
   unchanged.
3. Accepted failure classes: a descendant that leaves the group and drops the
   marker from its environment or changes user (ADR 0010); `SIGKILL` delivered to
   Mending itself skips cleanup (lease stays active, so the next run blocks); a
   second enabled `adept` plugin in the host account shadowing the configured one
   (ADR 0010 states the prerequisite).
4. Covered elsewhere: spending limits (#29); duplicate dispatch after a crash and
   attempt reruns (#30); composition, receipt mapping, and unit wiring (#31); live
   runs (#7).

### Threat model

- Boundaries added: config values become argv and a working directory; the
  brief (published issue text) becomes the host prompt; host stdout is parsed.
- Actors: the operator (trusted, owns config); issue authors (untrusted brief
  text); the host process (runs code under the account's permissions).
- Controls: argv list, no shell; the brief goes on stdin, never argv; JSON
  parsing only, no evaluation; host permissions from account settings, no
  bypass flag; output is not echoed into GitHub by this adapter; signals go only
  to the host's group and to PIDs whose environment carries this attempt's marker
  (a same-user process that copies the marker can already be signalled by that user).
- Out of scope: prompt injection in the brief steering the host within its
  granted permissions (owned by the brief review in #18 and host permissions);
  a malicious operator config.

## Success

- Each preflight reason in Behavior step 1 parks without launching the stand-in host.
- A successful stand-in yields `completed` with the derived session ID, the
  manifest version, and the parsed result; the stand-in receives the brief on
  stdin, the marker in its environment, and the expected argv.
- A hung stand-in with a `SIGTERM`-ignoring child in its group and a
  `SIGTERM`-ignoring child in a new session is stopped at the deadline, and all
  three PIDs are gone: `stopped`/`timeout`.
- When the tree cannot be verified empty, the outcome is `unknown`, never `stopped`.
- A stand-in that exits leaving a child: the child is stopped, outcome reflects
  the exit code.
- A `SIGTERM` to Mending while a hung stand-in runs raises `SystemExit` after the
  stand-in's tree is gone.
- Configs without the new keys decode as before.

## Validation

- Mode: focused-test — `desloppify/tests/commands/test_repair_cycle_host.py`,
  a Python stand-in host script in `tmp_path`, behavior chosen by env var.
- Mode: focused-test — config decoding in `test_repair_cycle.py`.
- Mode: task-test-not-applicable — ADR 0010 prose; no executable consumer.
