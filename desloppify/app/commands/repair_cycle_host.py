"""Launch one Claude Code host running the installed Adept skills (ADR 0010)."""

from __future__ import annotations

import json
import os
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
from pathlib import Path
from types import FrameType
from typing import Any

from desloppify.engine.repair_cycle import CycleConfig

MARKER_VARIABLE = "MENDING_HOST_SESSION"
_DETAIL_CHARS = 2_000
_POLL_SECONDS = 0.05
_CANCEL_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


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
    ``stopped`` (deadline reached, worker tree verified empty), or ``unknown``
    (worker tree could not be verified empty).
    """

    state: str
    reason: str | None = None
    session_id: str | None = None
    skills_version: str | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    detail: str | None = None


class ClaudeHostAdapter:
    """Run ``claude -p`` with the Adept plugin and stop its whole worker tree."""

    def __init__(self, config: CycleConfig, *, grace_seconds: float = 10.0) -> None:
        self._config = config
        self._grace_seconds = grace_seconds

    def run(self, request: HostRequest) -> HostOutcome:
        """Launch the host for one attempt and establish the state of its worker tree."""
        preflight = _preflight(self._config, request, datetime.now(request.deadline.tzinfo))
        if isinstance(preflight, HostOutcome):
            return preflight
        executable, skills_dir, version = preflight
        session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mending-attempt:{request.attempt_id}"))
        command = [
            executable, "-p", "--output-format", "json",
            "--model", str(self._config.model),
            "--session-id", session_id,
            "--plugin-dir", skills_dir,
        ]
        with tempfile.TemporaryDirectory(prefix="mending-host-") as scratch:
            files = Path(scratch)
            (files / "prompt").write_text(_prompt(request))
            with _cancellation_handlers() as cancellation:
                launched = self._launch(command, request, session_id, files, cancellation)
            if launched is None:
                return HostOutcome("parked", "host-launch-failed", session_id, version)
            exit_code, timed_out, emptied = launched
            stdout = (files / "stdout").read_text(errors="replace")
            stderr = (files / "stderr").read_text(errors="replace")
        if timed_out:
            state, reason = ("stopped", "timeout") if emptied else ("unknown", "timeout-survivors")
        elif not emptied:
            state, reason = "unknown", "worker-survivors"
        else:
            return _outcome_from_exit(exit_code, stdout, stderr, session_id, version)
        return HostOutcome(state, reason, session_id, version, exit_code)

    def _launch(
        self,
        command: list[str],
        request: HostRequest,
        session_id: str,
        files: Path,
        cancellation: _Cancellation,
    ) -> tuple[int | None, bool, bool] | None:
        """Return (exit code, timed out, tree verified empty), or None when nothing launched."""
        marker = f"{MARKER_VARIABLE}={session_id}".encode()
        with (
            (files / "prompt").open() as stdin,
            (files / "stdout").open("w") as stdout,
            (files / "stderr").open("w") as stderr,
        ):
            cancellation.raise_if_requested()
            try:
                proc = subprocess.Popen(  # nosec B603
                    command,
                    cwd=request.repo_root,
                    env={**os.environ, MARKER_VARIABLE: session_id},
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError:
                return None
            try:
                cancellation.arm()
                now = datetime.now(request.deadline.tzinfo)
                remaining = (request.deadline - now).total_seconds()
                try:
                    proc.wait(timeout=max(remaining, 0))
                    timed_out = False
                except subprocess.TimeoutExpired:
                    timed_out = True
                emptied = _stop_tree(proc, marker, self._grace_seconds)
            except BaseException:
                _stop_tree(proc, marker, self._grace_seconds)
                raise
            return proc.returncode, timed_out, emptied


def _preflight(
    config: CycleConfig, request: HostRequest, now: datetime
) -> tuple[str, str, str] | HostOutcome:
    if config.model is None:
        return HostOutcome("parked", "missing-model")
    executable = shutil.which(config.host_executable)
    if executable is None:
        return HostOutcome("parked", "missing-host")
    skills_dir, version = config.adept_skills_dir, config.adept_skills_version
    if skills_dir is not None:
        skills_dir = os.path.abspath(skills_dir)
    manifest = None if skills_dir is None or version is None else _manifest(skills_dir)
    if skills_dir is None or version is None or manifest is None:
        return HostOutcome("parked", "missing-host-skills")
    if manifest.get("name") != "adept" or manifest.get("version") != version:
        return HostOutcome("parked", "host-skills-mismatch")
    if now >= request.deadline:
        return HostOutcome("parked", "runtime-exhausted")
    return os.path.abspath(executable), skills_dir, version


def _manifest(skills_dir: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(Path(skills_dir, ".claude-plugin", "plugin.json").read_text())
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _prompt(request: HostRequest) -> str:
    return (
        "Use the installed Adept skills to claim, isolate, implement, verify, and review "
        "this repair. Stop at a draft pull request; do not merge.\n\n"
        f"Attempt ID: {request.attempt_id}\n"
        f"Source revision: {request.source_revision}\n"
        f"Authorized scope: {request.authorized_scope}\n\n"
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
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    found: set[int] = set()
    for entry in proc_root.iterdir():
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


def _outcome_from_exit(
    code: int | None, stdout: str, stderr: str, session_id: str, version: str
) -> HostOutcome:
    try:
        result = json.loads(stdout)
    except ValueError:
        result = None
    detail = stderr[-_DETAIL_CHARS:] or None
    if not isinstance(result, dict):
        return HostOutcome(
            "failed", "invalid-host-output", session_id, version, code, detail=detail
        )
    if code == 0 and result.get("is_error") is False:
        return HostOutcome("completed", None, session_id, version, code, result)
    return HostOutcome("failed", "host-error", session_id, version, code, result, detail)


__all__ = ["MARKER_VARIABLE", "ClaudeHostAdapter", "HostOutcome", "HostRequest"]
