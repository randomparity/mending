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
  authorized_scope: str, repo_root: Path, deadline: datetime)`.
- `HostOutcome(state, reason, session_id, skills_version, exit_code, result)`;
  `state` is one of `parked`, `completed`, `failed`, `stopped`, `unknown`.
- `ClaudeHostAdapter(config, *, clock=None, grace_seconds=10.0)`;
  `run(request) -> HostOutcome`.

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
   `start_new_session=True`, the prompt on stdin from a temp file, and stdout and
   stderr to temp files. `session_id` is `uuid5(NAMESPACE_URL,
   "mending-attempt:" + attempt_id)`. The prompt states the attempt ID, source
   revision, authorized scope, and brief, and instructs the host to use the
   installed Adept skills and stop at a draft pull request without merging.
3. Wait until the deadline. On timeout, stop the group: `SIGTERM`, poll up to
   `grace_seconds`, `SIGKILL`, poll up to `grace_seconds`; the group is empty when
   `os.killpg(pgid, 0)` raises `ProcessLookupError` after the leader is reaped.
   Empty → `stopped`/`timeout`; otherwise `unknown`/`timeout-survivors`.
4. On `BaseException` while waiting (e.g. `KeyboardInterrupt`), stop the group and
   re-raise. The persisted lease has no terminal receipt, so the next run treats
   the attempt as active.
5. After a normal exit, stop any remaining group members the same way; survivors →
   `unknown`/`worker-survivors`. Otherwise exit 0 with a JSON object whose
   `is_error` is false → `completed`; unparseable output → `failed`/
   `invalid-host-output`; anything else → `failed`/`host-error`. `result` is the
   parsed object or None.

An `unknown` outcome keeps the active attempt: `CycleState.begin` replaces a lease
only after a terminal receipt (existing rule), and #31 maps `unknown` to that path.

## Failure model

1. Actors and deployments: a local operator running a one-shot command; the
   systemd service from ADR 0006 under a dedicated account. Linux only.
2. Invariants and assets: no worker process left running unreported after
   `run` returns; no launch when host, skills, or model are missing; no
   permission bypass; existing config and state decode unchanged.
3. Accepted failure classes: a descendant that calls `setsid` escapes the group
   check (ADR 0010; systemd control-group kill is the timer-path backstop);
   `SIGKILL` delivered to Mending itself skips cleanup (lease stays active, so
   the next run blocks).
4. Covered elsewhere: spending limits (#29); duplicate dispatch after a crash
   (#30); composition and receipt mapping (#31); live runs (#7).

### Threat model

- Boundaries added: config values become argv and a working directory; the
  brief (published issue text) becomes the host prompt; host stdout is parsed.
- Actors: the operator (trusted, owns config); issue authors (untrusted brief
  text); the host process (runs code under the account's permissions).
- Controls: argv list, no shell; the brief goes on stdin, never argv; JSON
  parsing only, no evaluation; host permissions from account settings, no
  bypass flag; output is not echoed into GitHub by this adapter.
- Out of scope: prompt injection in the brief steering the host within its
  granted permissions (owned by the brief review in #18 and host permissions);
  a malicious operator config.

## Success

- Each preflight reason parks without launching the stand-in host.
- A successful stand-in yields `completed` with the derived session ID, the
  manifest version, and the parsed result; the stand-in receives the brief on stdin
  and the expected argv.
- A hung stand-in with a `SIGTERM`-ignoring child is stopped at the deadline and
  both PIDs are gone: `stopped`/`timeout`.
- When the group cannot be emptied, the outcome is `unknown`, never `stopped`.
- A stand-in that exits leaving a child: the child is stopped, outcome reflects
  the exit code.
- Configs without the new keys decode as before.

## Validation

- Mode: focused-test — `desloppify/tests/commands/test_repair_cycle_host.py`,
  a Python stand-in host script in `tmp_path`, behavior chosen by env var.
- Mode: focused-test — config decoding in `test_repair_cycle.py`.
- Mode: task-test-not-applicable — ADR 0010 prose; no executable consumer.
