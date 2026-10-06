"""Launch one Claude Code host running the installed Adept skills (ADR 0010)."""

from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import IO, Any

from desloppify.engine.repair_cycle import BudgetAdmission, CycleConfig

MARKER_VARIABLE = "MENDING_HOST_SESSION"
# The host is asked to put "<tag>: <attempt ID>" in every issue and PR body it creates.
ATTEMPT_TAG = "Mending-Attempt"
# Background subagents are not streamed, so their calls could not be counted.
BACKGROUND_TASKS_VARIABLE = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
# Subagent messages are forwarded at every nesting depth from this release.
_MIN_HOST_VERSION = (2, 1, 275)
_REQUIRED_OPTIONS = ("--max-budget-usd", "stream-json", "--forward-subagent-text")
_PROBE_SECONDS = 10.0
_PROC_ROOT = Path("/proc")
_DETAIL_CHARS = 2_000
_POLL_SECONDS = 0.05
_CANCEL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_LOOKUP_SECONDS = 10.0
_LOOKUP_LIMIT = "100"
_LOOKUP_DETAIL_CHARS = 200


def host_session_id(attempt_id: str) -> str:
    """Return the single-use host session ID derived from a durable attempt ID (ADR 0010)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mending-attempt:{attempt_id}"))


class HostLookupError(RuntimeError):
    """A lookup of what a dispatched attempt left behind could not be completed."""


@dataclass(frozen=True)
class HostReferences:
    """Issue and PR URLs carrying an attempt's tag, and worktrees created during it."""

    pull_requests: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequest:
    """One recorded pull request: whether it is open, and the paths it changes.

    ``files`` holds every changed path, including the old path of a renamed file.
    """

    url: str
    open: bool
    files: frozenset[str]


@dataclass(frozen=True)
class HostRequest:
    """An already selected, reviewed repair handed to the host."""

    attempt_id: str
    brief: str
    source_revision: str
    authorized_scope: str
    repo_root: Path
    deadline: datetime


@dataclass(frozen=True)
class HostOutcome:
    """What the adapter established about one host run.

    ``state`` is ``parked`` (nothing launched), ``completed``, ``failed``,
    ``stopped`` (deadline or call limit reached, worker tree verified empty), or
    ``unknown`` (worker tree could not be verified empty). ``cost_usd`` is the
    host-measured cost, or None when the host did not measure it; ``calls``
    counts streamed model responses.
    """

    state: str
    reason: str | None = None
    session_id: str | None = None
    skills_version: str | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    detail: str | None = None
    cost_usd: Decimal | None = None
    calls: int = 0


class _StreamUsage:
    """Count streamed model responses and keep the result event of a stream-json run."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._message_ids: set[str] = set()
        self._unidentified = 0
        self.result: dict[str, Any] | None = None

    def feed(self, data: bytes, *, final: bool = False) -> None:
        lines = (self._pending + self._decoder.decode(data, final)).split("\n")
        self._pending = "" if final else lines.pop()
        for line in lines:
            self._event(line)

    def _event(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "result":
            self.result = event
        elif event.get("type") == "assistant":
            message = event.get("message")
            message_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message_id, str) and message_id:
                self._message_ids.add(message_id)
            else:
                self._unidentified += 1

    @property
    def calls(self) -> int:
        return len(self._message_ids) + self._unidentified

    @property
    def cost_usd(self) -> Decimal | None:
        result = self.result
        # A crash result may report zeroed cost, so it measures nothing.
        if result is None or result.get("subtype") == "error_during_execution":
            return None
        cost = result.get("total_cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return None
        value = Decimal(str(cost))
        return value if value.is_finite() and value >= 0 else None


class ClaudeHostAdapter:
    """Run ``claude -p`` with the Adept plugin and stop its whole worker tree."""

    def __init__(self, config: CycleConfig, *, grace_seconds: float = 10.0) -> None:
        self._config = config
        self._grace_seconds = grace_seconds

    def run(self, request: HostRequest, admission: BudgetAdmission) -> HostOutcome:
        """Launch the host within an admitted budget and establish its worker tree's state."""
        preflight = _preflight(self._config, request, datetime.now(request.deadline.tzinfo))
        if isinstance(preflight, HostOutcome):
            return preflight
        executable, skills_dir, version = preflight
        session_id = host_session_id(request.attempt_id)
        command = [
            executable, "-p", "--output-format", "stream-json", "--verbose",
            "--forward-subagent-text", "--max-budget-usd", format(admission.cost_usd, "f"),
            "--model", str(self._config.model),
            "--session-id", session_id,
            "--plugin-dir", skills_dir,
        ]
        with tempfile.TemporaryDirectory(prefix="mending-host-") as scratch:
            files = Path(scratch)
            (files / "prompt").write_text(_prompt(request))
            with _cancellation_handlers() as cancellation:
                launched = self._launch(
                    command, request, admission, session_id, files, cancellation
                )
            if launched is None:
                return HostOutcome("parked", "host-launch-failed", session_id, version)
            exit_code, stop, emptied, usage = launched
            stderr = (files / "stderr").read_text(errors="replace")
        if stop is not None:
            state, reason = ("stopped", stop) if emptied else ("unknown", f"{stop}-survivors")
        elif not emptied:
            state, reason = "unknown", "worker-survivors"
        else:
            return _outcome_from_result(exit_code, usage, stderr, session_id, version)
        return HostOutcome(
            state, reason, session_id, version, exit_code,
            cost_usd=usage.cost_usd, calls=usage.calls,
        )

    @property
    def repository(self) -> str:
        """The configured repository whose issues and PRs the host may create."""
        return self._config.repository

    def worker_alive(self, attempt_id: str) -> bool | None:
        """Return whether any process carries the attempt's marker; None without /proc."""
        marked = _marked_pids(f"{MARKER_VARIABLE}={host_session_id(attempt_id)}".encode())
        return None if marked is None else bool(marked)

    def worktrees(self, repo_root: Path) -> tuple[str, ...]:
        """Return the repository's worktree paths."""
        listing = _output(
            "git worktree list", ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"]
        )
        return tuple(
            line.removeprefix("worktree ")
            for line in listing.splitlines()
            if line.startswith("worktree ")
        )

    def references(
        self,
        attempt_id: str,
        repository: str,
        repo_root: Path,
        prior_worktrees: tuple[str, ...],
    ) -> HostReferences:
        """Find the attempt's tagged issues and PRs and the worktrees created since launch."""
        tag = f"{ATTEMPT_TAG}: {attempt_id}"
        return HostReferences(
            pull_requests=_tagged("pr", repository, tag),
            issues=_tagged("issue", repository, tag),
            worktrees=tuple(
                path for path in self.worktrees(repo_root) if path not in prior_worktrees
            ),
        )

    def pull_requests(self, urls: tuple[str, ...]) -> tuple[PullRequest, ...]:
        """Read each recorded pull request of the configured repository.

        The URLs come from the state file, which the host can write, so each must
        be a pull request URL of this repository before it reaches ``gh``.
        """
        pattern = re.compile(
            rf"https://github\.com/{re.escape(self.repository)}/pull/[0-9]+", re.IGNORECASE
        )
        if bad := [url for url in urls if not pattern.fullmatch(url)]:
            raise HostLookupError(f"not a pull request of {self.repository}: {bad[0][:200]!r}")
        return tuple(_pull_request(self.repository, url) for url in urls)

    def _launch(
        self,
        command: list[str],
        request: HostRequest,
        admission: BudgetAdmission,
        session_id: str,
        files: Path,
        cancellation: _Cancellation,
    ) -> tuple[int | None, str | None, bool, _StreamUsage] | None:
        """Return (exit code, stop reason, tree verified empty, usage), or None if not launched."""
        marker = f"{MARKER_VARIABLE}={session_id}".encode()
        usage = _StreamUsage()
        with (
            (files / "prompt").open() as stdin,
            (files / "stdout").open("w") as stdout,
            (files / "stdout").open("rb") as stream,
            (files / "stderr").open("w") as stderr,
        ):
            cancellation.raise_if_requested()
            try:
                proc = subprocess.Popen(  # nosec B603
                    command,
                    cwd=request.repo_root,
                    env={
                        **os.environ,
                        MARKER_VARIABLE: session_id,
                        BACKGROUND_TASKS_VARIABLE: "1",
                    },
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError:
                return None
            try:
                cancellation.arm()
                stop = _watch(proc, request.deadline, admission.calls, usage, stream)
                emptied = _stop_tree(proc, marker, self._grace_seconds)
            except BaseException:
                _stop_tree(proc, marker, self._grace_seconds)
                raise
            usage.feed(stream.read(), final=True)
            return proc.returncode, stop, emptied, usage


def _watch(
    proc: subprocess.Popen[bytes],
    deadline: datetime,
    call_limit: int,
    usage: _StreamUsage,
    stream: IO[bytes],
) -> str | None:
    """Follow the host's stream until it exits; return why Mending must stop it, if it must."""
    while True:
        exited = proc.poll() is not None
        usage.feed(stream.read())
        if exited:
            return None
        if usage.calls > call_limit:
            return "call-limit"
        if datetime.now(deadline.tzinfo) >= deadline:
            return "timeout"
        time.sleep(_POLL_SECONDS)


def _preflight(
    config: CycleConfig, request: HostRequest, now: datetime
) -> tuple[str, str, str] | HostOutcome:
    if config.model is None:
        return HostOutcome("parked", "missing-model")
    executable = shutil.which(config.host_executable)
    if executable is None:
        return HostOutcome("parked", "missing-host")
    skills_dir, version = config.adept_skills_dir, config.adept_skills_version
    if skills_dir is None or version is None:
        return HostOutcome("parked", "missing-host-skills")
    skills_dir = os.path.abspath(skills_dir)
    manifest = _manifest(skills_dir)
    if manifest is None:
        return HostOutcome("parked", "missing-host-skills")
    if manifest.get("name") != "adept" or manifest.get("version") != version:
        return HostOutcome("parked", "host-skills-mismatch")
    if now >= request.deadline:
        return HostOutcome("parked", "runtime-exhausted")
    executable = os.path.abspath(executable)
    if not _limits_enforceable(executable):
        return HostOutcome("parked", "unenforceable-limit")
    return executable, skills_dir, version


def _limits_enforceable(executable: str) -> bool:
    """Return whether this host and platform can enforce the cost, call, and runtime limits."""
    if not _PROC_ROOT.is_dir():
        return False
    version = _probe(executable, "--version")
    usage = _probe(executable, "--help")
    if version is None or usage is None:
        return False
    found = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", version)
    if found is None or tuple(int(part) for part in found.groups()) < _MIN_HOST_VERSION:
        return False
    return all(option in usage for option in _REQUIRED_OPTIONS)


def _probe(executable: str, flag: str) -> str | None:
    """Return a model-free probe's output, or None when it fails or times out."""
    try:
        done = subprocess.run(  # nosec B603
            [executable, flag], capture_output=True, text=True, timeout=_PROBE_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _manifest(skills_dir: str) -> dict[str, Any] | None:
    """Return the plugin manifest, or None when it or every packaged skill is missing."""
    if not any(Path(skills_dir, "skills").glob("*/SKILL.md")):
        return None
    try:
        raw = json.loads(Path(skills_dir, ".claude-plugin", "plugin.json").read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _tagged(kind: str, repository: str, tag: str) -> tuple[str, ...]:
    """Return URLs of the repository's recent issues or PRs whose body has the tag line."""
    output = _output(f"gh {kind} list", [
        "gh", kind, "list", "--repo", repository, "--state", "all",
        "--limit", _LOOKUP_LIMIT, "--json", "url,body",
    ])
    try:
        entries = json.loads(output)
    except ValueError as exc:
        raise HostLookupError(f"gh {kind} list returned invalid JSON") from exc
    if not isinstance(entries, list):
        raise HostLookupError(f"gh {kind} list did not return a list")
    return tuple(
        entry["url"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("url"), str)
        and isinstance(entry.get("body"), str)
        and tag in entry["body"].splitlines()
    )


def _pull_request(repository: str, url: str) -> PullRequest:
    """Read a pull request's state and every path it changes, renames' old paths included.

    The file list must account for the pull request's whole changed-file count;
    one it cannot fully list (the API returns at most 3000 files) fails closed.
    """
    endpoint = f"repos/{repository}/pulls/{url.rsplit('/', 1)[1]}"
    found = _api_json(["gh", "api", endpoint])
    pages = _api_json(["gh", "api", "--paginate", "--slurp", f"{endpoint}/files?per_page=100"])
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise HostLookupError("gh api returned an unexpected file list")
    entries = [entry for page in pages for entry in page]
    state = found.get("state") if isinstance(found, dict) else None
    count = found.get("changed_files") if isinstance(found, dict) else None
    if (
        not isinstance(state, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or not all(_file_entry(entry) for entry in entries)
    ):
        raise HostLookupError("gh api returned an unexpected pull request shape")
    if len(entries) != count:
        raise HostLookupError(f"gh api listed {len(entries)} of {count} changed files")
    paths = {entry["filename"] for entry in entries}
    paths.update(entry["previous_filename"] for entry in entries if entry.get("previous_filename"))
    return PullRequest(url, state == "open", frozenset(paths))


def _file_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("filename"), str)
        and isinstance(entry.get("previous_filename", ""), (str, type(None)))
    )


def _api_json(argv: list[str]) -> object:
    try:
        return json.loads(_output("gh api", argv))
    except ValueError as exc:
        raise HostLookupError("gh api returned invalid JSON") from exc


def _output(label: str, argv: list[str]) -> str:
    """Run a fixed lookup command and return its stdout; raise HostLookupError on failure."""
    executable = shutil.which(argv[0])
    if executable is None:
        raise HostLookupError(f"{label}: {argv[0]} is not installed")
    try:
        done = subprocess.run(  # nosec B603
            [executable, *argv[1:]],
            capture_output=True, text=True, timeout=_LOOKUP_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostLookupError(f"{label} could not run: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.strip()[-_LOOKUP_DETAIL_CHARS:]
        raise HostLookupError(f"{label} exited {done.returncode}: {detail}")
    return done.stdout


def _prompt(request: HostRequest) -> str:
    return (
        "Use the installed Adept skills to claim, isolate, implement, verify, and review "
        "this repair. Stop at a draft pull request; do not merge.\n\n"
        f"Attempt ID: {request.attempt_id}\n"
        f"Source revision: {request.source_revision}\n"
        f"Authorized scope: {request.authorized_scope}\n"
        f"Put the line '{ATTEMPT_TAG}: {request.attempt_id}' in the body of every issue "
        "and pull request you create.\n\n"
        f"Brief:\n{request.brief}\n"
    )


class _Cancellation:
    """Defer a cancellation signal until the host's process can be stopped.

    A signal that arrives while ``Popen`` runs is recorded and raised once the
    process handle exists, so the host is never orphaned. The first signal also
    ignores further cancellation signals, so cleanup runs to completion.
    """

    def __init__(self) -> None:
        self.signum: int | None = None
        self.armed = False

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        for sig in _CANCEL_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        self.signum = signum
        if self.armed:
            self.raise_if_requested()

    def arm(self) -> None:
        self.armed = True
        self.raise_if_requested()

    def raise_if_requested(self) -> None:
        if self.signum == signal.SIGINT:
            raise KeyboardInterrupt
        if self.signum is not None:
            raise SystemExit(128 + self.signum)


@contextmanager
def _cancellation_handlers() -> Iterator[_Cancellation]:
    """Route SIGINT/SIGTERM/SIGHUP through one cancellation while the host runs.

    Handlers can only be installed on the main thread; elsewhere the returned
    cancellation never fires.
    """
    cancellation = _Cancellation()
    if threading.current_thread() is not threading.main_thread():
        yield cancellation
        return
    previous = {sig: signal.getsignal(sig) for sig in _CANCEL_SIGNALS}
    for sig in _CANCEL_SIGNALS:
        signal.signal(sig, cancellation.handle)
    try:
        yield cancellation
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _marked_pids(marker: bytes) -> set[int] | None:
    """Return PIDs whose environment carries the marker; None when /proc is unavailable."""
    if not _PROC_ROOT.is_dir():
        return None
    found: set[int] = set()
    for entry in _PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if marker in environ.split(b"\0"):
            found.add(int(entry.name))
    return found


def _signal_tree(pgid: int, marked: set[int], sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in marked:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            continue


def _tree_alive(pgid: int, marker: bytes) -> bool:
    marked = _marked_pids(marker)
    if marked is None or marked:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_tree(proc: subprocess.Popen[bytes], marker: bytes, grace_seconds: float) -> bool:
    """Signal the host's group and marked processes; True once the tree is verified empty."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if _settled(proc, marker, 0):
            return True
        _signal_tree(proc.pid, _marked_pids(marker) or set(), sig)
        if _settled(proc, marker, grace_seconds):
            return True
    return False


def _settled(proc: subprocess.Popen[bytes], marker: bytes, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while True:
        proc.poll()
        if not _tree_alive(proc.pid, marker):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(_POLL_SECONDS)


def _outcome_from_result(
    code: int | None, usage: _StreamUsage, stderr: str, session_id: str, version: str
) -> HostOutcome:
    result = usage.result
    if result is None:
        state, reason = "failed", "invalid-host-output"
    elif code == 0 and result.get("is_error") is False:
        state, reason = "completed", None
    else:
        state, reason = "failed", "host-error"
    return HostOutcome(
        state, reason, session_id, version, code, result,
        None if state == "completed" else stderr[-_DETAIL_CHARS:] or None,
        cost_usd=usage.cost_usd, calls=usage.calls,
    )


__all__ = [
    "ATTEMPT_TAG",
    "MARKER_VARIABLE",
    "ClaudeHostAdapter",
    "HostLookupError",
    "HostOutcome",
    "HostReferences",
    "HostRequest",
    "PullRequest",
    "host_session_id",
]
