"""The composed repair cycle against a scripted host and a fake ``gh`` (#32).

Each test runs ``cmd_repair_cycle`` with no injected host or GitHub client, so
the production ``ClaudeHostAdapter`` and ``GitHubIssueClient`` run against
``repair_cycle_stand_in.py``. Only the checkout reader, the evidence check,
and ``scan`` stay injected; their own tests cover them. Every process a test
starts carries ``STAND_IN_WORLD=<its tmp_path>``, which is how teardown finds
(and fails on) any that outlived the test without touching another test's.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from desloppify.app.commands import repair_cycle, repair_cycle_host
from desloppify.app.commands.repair_cycle import cmd_repair_cycle
from desloppify.app.commands.repair_cycle_host import ClaudeHostAdapter, host_session_id
from desloppify.base.exception_sets import CommandError
from desloppify.engine.repair_check import CheckResult
from desloppify.tests.commands.test_repair_cycle import _authority, _config
from desloppify.tests.commands.test_repair_queue import (
    MANIFEST,
    PASS,
    REPOSITORY,
    _manifest,
    _revalidated_state,
    _Source,
)

STAND_IN = Path(__file__).with_name("repair_cycle_stand_in.py")
MODULE = "desloppify.tests.commands.test_repair_cycle_e2e"
CHECKOUT = Path(__file__).resolve().parents[3]
WAIT_SECONDS = 20.0
DAY = timedelta(days=1)
STALE = _manifest("e" * 40)
OK_RUN = ({"do": "assistant", "count": 2}, {"do": "pr", "files": ["src/impl.py"]},
          {"do": "result", "cost": 0.4})
# Sync reads the source three times and selection once; the fifth read is the
# dispatch-time recheck, made after the lease is persisted.
DISPATCH_RECHECK = 5


def _next_noon_offset() -> timedelta:
    """Put the test clock at the next UTC noon, so no run straddles a window boundary."""
    now = datetime.now(UTC)
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    return (noon if noon > now else noon + DAY) - now


def _wait_for(condition: Callable[[], object], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.02)


class _HeldSource(_Source):
    """A checkout whose ``hold``-th read waits for the world's ``go`` file."""

    def __init__(self, root: Path, hold: int) -> None:
        super().__init__()
        self.root, self.hold = root, hold

    def manifest_for(self, issue):
        if self.calls + 1 == self.hold:
            (self.root / "held").touch()
            _wait_for((self.root / "go").exists, "release")
        return super().manifest_for(issue)


class _World:
    """One test's fake GitHub, scripted host, checkout, and cycle state."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.offset = _next_noon_offset()
        self.late = False  # see the late_clock fixture
        self.source: _Source = _Source()
        self.check: Callable[..., CheckResult] = lambda issue, manifest: PASS
        self.config: dict = {}

    @classmethod
    def build(cls, root: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
        world = cls(root)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for role in ("claude", "gh"):
            wrapper = bin_dir / role
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" {role} "$@"\n')
            wrapper.chmod(0o755)
        skills = root / "adept"
        (skills / ".claude-plugin").mkdir(parents=True)
        (skills / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "adept", "version": "7.2.0"})
        )
        (skills / "skills" / "quest").mkdir(parents=True)
        (skills / "skills" / "quest" / "SKILL.md").write_text("# quest\n")
        world.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=world.repo, check=True)
        (root / "world.json").write_text(
            json.dumps({"repository": REPOSITORY, "issues": [], "prs": [], "log": []})
        )
        (root / "state.json").write_text(json.dumps(_revalidated_state()))
        world.host(*OK_RUN)
        world.config = _config(
            authority=world.authority(),
            host_executable=str(bin_dir / "claude"),
            adept_skills_dir=str(skills),
            adept_skills_version="7.2.0",
        )
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("STAND_IN_WORLD", str(root))
        monkeypatch.setenv("GH_CONFIG_DIR", str(root / "gh-config"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "claude-config"))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        return world

    def now(self, tz=UTC) -> datetime:
        """The one test clock, shared by the cycle and the adapter."""
        late = self.late and (self.root / "pids.jsonl").exists()
        return (datetime.now(UTC) + self.offset + (timedelta(hours=1) if late else timedelta(0))
                ).astimezone(tz)

    def authority(self, **overrides: object) -> dict[str, object]:
        expires = (self.now() + 30 * DAY).isoformat()
        return _authority(**{"expires_at": expires, **overrides})

    def use_clock(self, patch: Callable[[object, str, object], None]) -> None:
        """Give the adapter this world's clock; the cycle gets it through ``args.clock``."""
        world = self

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return world.now(tz)

        patch(repair_cycle_host, "datetime", _Clock)

    def host(self, *steps: dict) -> None:
        (self.root / "host.json").write_text(json.dumps(list(steps)))

    def args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "command": "repair-cycle",
            "config": None,
            "config_data": self.config,
            "state": str(self.root / "state.json"),
            "state_data": None,
            "refresh": lambda: None,
            "repo_root": self.repo,
            "source": self.source,
            "check": self.check,
            "clock": self.now,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def run(self, **overrides: object) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_repair_cycle(self.args(**overrides))
        return out.getvalue()

    def drive(self, hold: int = 0) -> subprocess.Popen[bytes]:
        """Run one cycle in a child Python process, which a test may kill."""
        (self.root / "drive.json").write_text(json.dumps(
            {"config": self.config, "offset": self.offset.total_seconds(), "hold": hold}
        ))
        with (self.root / "drive.out").open("w") as out:
            return subprocess.Popen(
                [sys.executable, "-c", f"from {MODULE} import _drive; _drive()", str(self.root)],
                cwd=CHECKOUT, env={**os.environ, "PYTHONPATH": str(CHECKOUT)},
                stdout=out, stderr=subprocess.STDOUT,
            )

    def kill_drive(self, cycle: subprocess.Popen[bytes]) -> None:
        cycle.kill()
        cycle.wait()

    def cycle(self) -> dict:
        return json.loads((self.root / "state.json").read_text())["repair_cycle"]

    def github(self) -> dict:
        with (self.root / "world.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return json.loads((self.root / "world.json").read_text())

    def merge_repair(self) -> None:
        """Merge every PR and close the repair issue, as a merged ``Fixes`` PR would."""
        with (self.root / "world.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = self.root / "world.json"
            world = json.loads(path.read_text())
            for item in world["prs"]:
                item["state"] = "MERGED"
            for item in world["issues"]:
                item["state"] = "CLOSED"
            path.write_text(json.dumps(world))

    def creates(self, kind: str) -> int:
        return sum(argv[:2] == [kind, "create"] for argv in self.github()["log"])

    def launches(self) -> list[dict]:
        path = self.root / "launches.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def spawned(self) -> list[int]:
        path = self.root / "pids.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [json.loads(line)["pid"] for line in lines]

    def processes(self) -> list[int]:
        """Live, non-zombie processes started for this world, other than this one."""
        marker = f"STAND_IN_WORLD={self.root}".encode()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                environ = (entry / "environ").read_bytes()
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except (OSError, IndexError):
                continue
            if state != "Z" and marker in environ.split(b"\0"):
                found.append(int(entry.name))
        return found

    def release(self) -> None:
        (self.root / "go").touch()
        _wait_for(lambda: not self.processes(), "this world's processes to exit")


def _drive() -> None:
    root = Path(sys.argv[1])
    spec = json.loads((root / "drive.json").read_text())
    world = _World(root)
    world.config, world.offset = spec["config"], timedelta(seconds=spec["offset"])
    world.use_clock(setattr)
    if spec["hold"]:
        world.source = _HeldSource(root, spec["hold"])
    print(world.run(), flush=True)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    built = _World.build(tmp_path, monkeypatch)
    built.use_clock(monkeypatch.setattr)
    yield built
    survivors = built.processes()
    for pid in survivors:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert survivors == [], "a process started by this test outlived it"


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production adapter with a 0.5 s grace, so SIGTERM-ignoring trees stop fast."""
    monkeypatch.setattr(
        repair_cycle, "ClaudeHostAdapter", functools.partial(ClaudeHostAdapter, grace_seconds=0.5)
    )


@pytest.fixture
def late_clock(world: _World) -> None:
    """The test clock jumps an hour once the host has spawned a child.

    A deadline then passes at a known point of the run instead of after a
    real-time margin that a slow machine could miss.
    """
    world.late = True


# 1. No-op / discovery only.
def test_discovery_only_publishes_once_and_never_launches(world) -> None:
    world.config["authority"] = None

    assert "Repair cycle no-op: authority-missing." in world.run()
    assert "Repair cycle no-op: authority-missing." in world.run()

    assert world.cycle()["current_lease"] is None
    assert (world.launches(), world.creates("issue")) == ([], 1)


# 2. Approved small repair.
def test_approved_repair_runs_once_through_the_adapter_and_settles(world) -> None:
    assert "Repair cycle active: pull request open." in world.run()

    [launch] = world.launches()
    attempt = world.cycle()["current_lease"]["attempt_id"]
    argv = launch["argv"]
    assert launch["attempt"] == attempt
    assert argv[argv.index("--session-id") + 1] == launch["marker"] == host_session_id(attempt)
    assert argv[argv.index("--max-budget-usd") + 1] == "2.00"
    assert argv[argv.index("--plugin-dir") + 1] == str(world.root / "adept")
    assert not {"--permission-mode", "--dangerously-skip-permissions", "--settings"} & set(argv)
    assert launch["config_dir"] == str(world.root / "claude-config")
    assert launch["background"] == "1"
    [pr] = world.github()["prs"]
    assert ["pr", "view", pr["url"], "--json", "state,files"] in world.github()["log"]
    dispatch = world.cycle()["dispatch"]
    assert (dispatch["outcome"], dispatch["pull_requests"]) == ("completed", [pr["url"]])
    assert world.cycle()["consumed_cost_usd"] == "0.4"

    world.merge_repair()
    out = world.run()
    assert "Repair cycle settled." in out
    assert "Repair cycle parked: window-attempt-complete." in out
    world.offset += DAY
    assert "Repair cycle no-op: selected-repair-unavailable." in world.run()

    assert (len(world.launches()), world.creates("issue"), world.creates("pr")) == (1, 1, 1)


# 3. Missing host or skills.
@pytest.mark.parametrize(("overrides", "reason"), [
    ({"host_executable": "/nonexistent/claude"}, "missing-host"),
    ({"adept_skills_dir": "/nonexistent"}, "missing-host-skills"),
    ({"adept_skills_version": "7.1.0"}, "host-skills-mismatch"),
])
def test_missing_host_or_skills_parks_without_launch(world, overrides, reason) -> None:
    world.config.update(overrides)

    assert f"Repair cycle parked: {reason}." in world.run()

    assert world.cycle()["dispatch"] is None
    assert (world.launches(), world.creates("pr")) == ([], 0)


# 4. Permission denial and revocation.
def test_permission_denied_host_fails_without_a_pull_request(world) -> None:
    # The host's settings refused the push, so it finished without opening a PR.
    world.host({"do": "assistant", "count": 1}, {"do": "result"})

    assert "Repair cycle parked: no-pull-request." in world.run()
    attempt = world.cycle()["current_lease"]["attempt_id"]
    world.offset += DAY
    assert "Repair cycle parked: no-pull-request." in world.run()
    with pytest.raises(CommandError, match="confirm-stopped"):
        world.run(dispose_attempt=attempt)

    assert world.cycle()["attempt_failure"] == "no-pull-request"
    assert (len(world.launches()), world.creates("pr")) == (1, 0)


@pytest.mark.parametrize(("change", "reason"), [
    ({"revoked": True}, "authority-revoked"),
    ({"expires_at": "2026-01-01T00:00:00+00:00"}, "authority-expired"),
])
def test_revoked_or_expired_attempt_fails_on_resume_and_never_relaunches(
    world, change, reason
) -> None:
    world.run()
    attempt = world.cycle()["current_lease"]["attempt_id"]
    world.config["authority"] = world.authority(**change)

    assert f"Repair cycle parked: {reason}." in world.run()
    world.offset += DAY
    assert f"Repair cycle parked: {reason}." in world.run()
    with pytest.raises(CommandError, match="confirm-stopped"):
        world.run(dispose_attempt=attempt)

    assert world.cycle()["attempt_failure"] == reason
    assert (len(world.launches()), world.creates("pr")) == (1, 1)


def test_expired_approval_never_launches(world) -> None:
    world.config["authority"] = world.authority(expires_at="2026-01-01T00:00:00+00:00")

    assert "Repair cycle parked: authority-expired." in world.run()

    assert world.cycle()["current_lease"] is None
    assert world.launches() == []


# 5. Stale evidence or base.
def test_stale_evidence_at_sync_is_a_no_op(world) -> None:
    world.check = lambda issue, manifest: CheckResult("fail", "anchor moved", ())

    assert "Repair cycle no-op: selected-repair-unavailable." in world.run()

    assert (world.launches(), world.creates("issue")) == ([], 0)


def test_base_moving_before_dispatch_parks_without_launch(world) -> None:
    world.source = _Source(*[MANIFEST] * (DISPATCH_RECHECK - 1), STALE)

    assert "Repair cycle parked: source-not-current." in world.run()

    assert world.source.calls == DISPATCH_RECHECK
    assert world.cycle()["current_lease"] is not None
    assert world.cycle()["dispatch"] is None
    assert world.launches() == []


def test_base_moving_under_an_open_pull_request_fails_the_attempt(world) -> None:
    world.run()
    world.source = _Source(STALE)

    assert "Repair cycle parked: source-not-current." in world.run()

    assert world.cycle()["attempt_failure"] == "source-not-current"
    assert len(world.launches()) == 1


# 6. Overlap.
def test_overlapping_invocation_leaves_the_running_cycle_alone(world) -> None:
    world.host({"do": "wait"}, *OK_RUN)
    first = world.drive()
    _wait_for(world.launches, "the first cycle's host")
    before = (world.root / "state.json").read_bytes()

    assert "Repair cycle already running." in world.run()

    assert (world.root / "state.json").read_bytes() == before
    world.release()
    assert first.wait(timeout=WAIT_SECONDS) == 0
    assert "Repair cycle active: pull request open." in (world.root / "drive.out").read_text()
    assert (len(world.launches()), world.creates("pr")) == (1, 1)


# 7. Nested budget exhaustion.
@pytest.mark.parametrize(("steps", "outcome", "reason"), [
    # Forwarded subagent messages count; the watcher stops the tree past the limit.
    (({"do": "assistant", "count": 2}, {"do": "assistant", "count": 6, "nested": True},
      {"do": "hang"}), "stopped", "budget-exhausted"),
    # The host reports spending past the cap: charged, then failed after the run.
    (({"do": "assistant", "count": 1}, {"do": "result", "cost": 2.5}),
     "completed", "budget-exhausted"),
    # The host's own --max-budget-usd stop.
    (({"do": "assistant", "count": 1},
      {"do": "result", "is_error": True, "subtype": "error_max_budget_usd", "cost": 2.0},
      {"do": "exit", "code": 1}), "failed", "host-failed"),
], ids=["call-limit", "cost-overrun", "host-budget"])
def test_budget_limits_fail_the_attempt(world, short_grace, steps, outcome, reason) -> None:
    world.config["call_limit"] = 5
    world.host(*steps)

    assert f"Repair cycle parked: {reason}." in world.run()
    world.offset += DAY
    assert "Repair cycle parked:" in world.run()

    assert world.cycle()["dispatch"]["outcome"] == outcome
    assert world.cycle()["attempt_failure"] == reason
    assert len(world.launches()) == 1


def test_a_nested_model_process_is_stopped_but_not_counted(world) -> None:
    world.host({"do": "assistant", "count": 2}, {"do": "nested", "count": 50}, *OK_RUN[1:])

    assert "Repair cycle active: pull request open." in world.run()

    assert world.cycle()["consumed_calls"] == 2
    assert (world.root / "nested.jsonl").read_text().count('"assistant"') == 50
    assert len(world.spawned()) == 1
    assert world.processes() == []


# 8. Timeout with a surviving child.
def test_timeout_stops_a_surviving_child_and_never_relaunches(
    world, short_grace, late_clock
) -> None:
    world.config["runtime_minutes"] = 30  # the clock jumps past it once the child exists
    world.host({"do": "child", "setsid": True, "ignore_term": True}, {"do": "hang"})

    assert "Repair cycle parked: runtime-exhausted." in world.run()
    assert len(world.spawned()) == 1
    assert world.processes() == []

    world.offset += DAY
    assert "Repair cycle parked: disposition-required." in world.run()
    assert len(world.launches()) == 1


# 9. Crash before or after dispatch or PR creation.
def test_a_cycle_killed_before_dispatch_launches_nothing(world) -> None:
    cycle = world.drive(hold=DISPATCH_RECHECK)
    _wait_for((world.root / "held").exists, "the dispatch recheck")
    world.kill_drive(cycle)
    world.release()
    assert world.cycle()["current_lease"] is not None and world.cycle()["dispatch"] is None

    # Nothing was launched, so the attempt is settled; its window is spent.
    assert "Repair cycle parked: window-attempt-complete." in world.run()
    world.offset += DAY
    assert "Repair cycle active: pull request open." in world.run()

    assert (len(world.launches()), world.creates("pr")) == (1, 1)


def test_a_cycle_killed_after_intent_before_launch_never_launches(world) -> None:
    (world.root / "hold-probe").touch()
    cycle = world.drive()
    _wait_for((world.root / "probing").exists, "the host probe")
    world.kill_drive(cycle)
    world.release()
    assert world.cycle()["dispatch"]["phase"] == "intent"

    assert "Repair cycle parked: unsettled-reservation." in world.run()
    world.offset += DAY
    assert "Repair cycle parked: unsettled-reservation." in world.run()

    assert world.launches() == []


@pytest.mark.parametrize("pr_first", [False, True], ids=["before-pr", "after-pr"])
def test_a_cycle_killed_during_its_host_never_relaunches_it(world, pr_first) -> None:
    hold = {"do": "wait"}
    world.host(*((OK_RUN[1], hold, OK_RUN[2]) if pr_first else (hold, *OK_RUN)))
    cycle = world.drive()
    _wait_for(lambda: world.github()["prs"] if pr_first else world.launches(), "the host")
    world.kill_drive(cycle)
    first = world.cycle()["current_lease"]["attempt_id"]
    assert world.cycle()["dispatch"]["phase"] == "intent"

    assert "Repair cycle parked: dispatch-in-flight." in world.run()
    world.release()
    assert "Repair cycle parked: unsettled-reservation." in world.run()
    [pr] = world.github()["prs"]
    assert world.cycle()["dispatch"]["pull_requests"] == [pr["url"]]

    # 11. Restart after lease expiry: still only observed, until a disposition.
    world.offset += 2 * DAY
    world.run()
    world.run()
    assert "Repair cycle parked: observation-exhausted." in world.run()
    world.run(dispose_attempt=first, confirm_stopped=True)
    assert "Repair cycle active: pull request open." in world.run()

    second = world.cycle()["current_lease"]["attempt_id"]
    assert world.cycle()["attempt_history"][-1]["lease"]["attempt_id"] == first
    assert [launch["attempt"] for launch in world.launches()] == [first, second]
    assert len({launch["marker"] for launch in world.launches()}) == 2
    tagged = [item["body"].count(first) for item in world.github()["prs"]]
    assert sorted(tagged) == [0, 1]  # one PR per attempt


@pytest.mark.parametrize("pr_first", [False, True], ids=["before-pr", "after-pr"])
def test_a_crashed_host_fails_the_attempt(world, pr_first) -> None:
    crash = ({"do": "result", "is_error": True, "subtype": "error_during_execution", "cost": 0},
             {"do": "exit", "code": 1})
    world.host(*((OK_RUN[1],) if pr_first else ()), *crash)

    assert "Repair cycle parked: host-failed." in world.run()

    assert len(world.cycle()["dispatch"]["pull_requests"]) == int(pr_first)
    assert world.cycle()["consumed_cost_usd"] == "2.00"  # an unmeasured crash charges it all
    attempt = world.cycle()["current_lease"]["attempt_id"]
    world.offset += DAY
    world.run()
    if pr_first:  # its pull request may still be merged, so the attempt stays active
        with pytest.raises(CommandError, match="confirm-stopped"):
            world.run(dispose_attempt=attempt)
    else:
        world.run(dispose_attempt=attempt)
    assert len(world.launches()) == 1


# 10. Mismatched or unknown results.
@pytest.mark.parametrize(("steps", "reason", "recorded"), [
    (({"do": "garbage"},), "host-failed", 0),
    (({"do": "result", "is_error": True},), "host-failed", 0),
    (({"do": "pr", "attempt": "another-attempt"}, {"do": "result"}), "no-pull-request", 0),
    (({"do": "pr", "files": ["src/impl.py", "deploy/secrets.env"]}, {"do": "result"}),
     "authority-scope-exceeded", 1),
], ids=["garbage", "error-result", "foreign-tag", "outside-approval"])
def test_mismatched_results_fail_the_attempt(world, steps, reason, recorded) -> None:
    world.host(*steps)

    assert f"Repair cycle parked: {reason}." in world.run()

    assert len(world.cycle()["dispatch"]["pull_requests"]) == recorded
    assert len(world.launches()) == 1


def test_an_unverifiable_worker_stays_reported_active(world, short_grace, monkeypatch) -> None:
    monkeypatch.setattr(repair_cycle_host, "_tree_alive", lambda _pgid, _marker: True)
    world.host({"do": "result"})

    assert "Repair cycle parked: dispatch-outcome-unknown." in world.run()
    attempt = world.cycle()["current_lease"]["attempt_id"]
    world.offset += DAY
    assert "Repair cycle parked: dispatch-outcome-unknown." in world.run()
    with pytest.raises(CommandError, match="confirm-stopped"):
        world.run(dispose_attempt=attempt)

    assert len(world.launches()) == 1


# 11. Restart reconciliation after lease expiry.
def test_an_open_pull_request_holds_the_attempt_past_its_lease(world) -> None:
    world.run()
    attempt = world.cycle()["current_lease"]["attempt_id"]
    world.offset += 2 * DAY  # past the lease deadline and into a later window

    assert "Repair cycle active: pull request open." in world.run()
    assert world.cycle()["current_lease"]["attempt_id"] == attempt

    world.merge_repair()
    out = world.run()
    assert "Repair cycle settled." in out
    assert "Repair cycle no-op: selected-repair-unavailable." in out
    assert (len(world.launches()), world.creates("issue"), world.creates("pr")) == (1, 1, 1)
