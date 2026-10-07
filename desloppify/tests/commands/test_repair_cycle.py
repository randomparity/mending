from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from desloppify.app.commands import repair_cycle
from desloppify.app.commands.repair_cycle import _dispatch_host, cmd_repair_cycle
from desloppify.app.commands.repair_cycle_host import (
    HostLookupError,
    HostOutcome,
    HostReferences,
    HostRequest,
    PullRequest,
    host_session_id,
)
from desloppify.base.exception_sets import CommandError
from desloppify.cli import create_parser
from desloppify.engine.repair_authority import (
    AuthorityBinding,
    UnsupportedAuthority,
    authority_from_mapping,
    check_authority,
)
from desloppify.engine.repair_brief import reviewed_version
from desloppify.engine.repair_cycle import (
    BudgetAdmission,
    CycleConfig,
    CycleState,
    DispatchRecord,
)
from desloppify.engine.repair_manifest import AnalysisUnknown
from desloppify.engine.repair_queue import (
    GitHubIssue,
    candidate_from_issue,
    concern_key,
)
from desloppify.tests.commands.test_repair_queue import (
    BASE,
    EVIDENCE,
    KEY,
    KEY_BODY,
    PASS,
    REPOSITORY,
    _revalidated_state,
    _Source,
)

NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
ITEM = "concerns::item"
PR_URL = "https://github.com/owner/repository/pull/9"


def _version() -> str:
    issue = _revalidated_state()["work_items"][ITEM]
    version = reviewed_version(issue, candidate_from_issue(issue, REPOSITORY))
    assert version is not None
    return version


VERSION = _version()


class _FakeHost:
    repository = REPOSITORY

    def __init__(self, outcome: HostOutcome | None = None, *, error: BaseException | None = None,
                 on_run=None, alive: bool | None = False, stops: bool = True,
                 found: HostReferences | HostLookupError | None = None,
                 snapshot: tuple[str, ...] | HostLookupError = ("/repo",),
                 on_references=None,
                 prs: tuple[PullRequest, ...] | HostLookupError | None = None,
                 events: list[str] | None = None) -> None:
        self.outcome = outcome or HostOutcome("completed", cost_usd=Decimal("0.5"), calls=3)
        self.error = error
        self.on_run = on_run
        self.alive = alive
        self.stops = stops
        self.stopped: list[str] = []
        self.found = found or HostReferences((PR_URL,))
        self.snapshot = snapshot
        self.on_references = on_references
        self.prs = prs if prs is not None else (_pr(),)
        self.events = events if events is not None else []
        self.admissions: list[BudgetAdmission] = []
        self.requests: list[HostRequest] = []
        self.lookups: list[tuple[str, str, Path, tuple[str, ...]]] = []
        self.pr_reads: list[tuple[str, ...]] = []

    def worker_alive(self, attempt_id: str) -> bool | None:
        return self.alive

    def stop_worker(self, attempt_id: str) -> bool:
        self.stopped.append(attempt_id)
        self.alive = not self.stops
        return self.stops

    def worktrees(self, repo_root: Path) -> tuple[str, ...]:
        if isinstance(self.snapshot, HostLookupError):
            raise self.snapshot
        return self.snapshot

    def references(self, attempt_id, repository, repo_root, prior_worktrees) -> HostReferences:
        self.lookups.append((attempt_id, repository, repo_root, prior_worktrees))
        if self.on_references is not None:
            self.on_references()
        if isinstance(self.found, HostLookupError):
            raise self.found
        return self.found

    def pull_requests(self, urls: tuple[str, ...]) -> tuple[PullRequest, ...]:
        self.pr_reads.append(urls)
        if isinstance(self.prs, HostLookupError):
            raise self.prs
        return self.prs if urls else ()

    def run(self, request: HostRequest, admission: BudgetAdmission) -> HostOutcome:
        self.events.append("run")
        self.admissions.append(admission)
        self.requests.append(request)
        if self.on_run is not None:
            self.on_run()
        if self.error is not None:
            raise self.error
        return self.outcome


def _pr(open: bool = True, files: tuple[str, ...] = ("src/impl.py",)) -> PullRequest:
    return PullRequest(PR_URL, open, frozenset(files))


class _Queue:
    """Fake GitHub client for the in-process repair-queue sync."""

    def __init__(self, issue_state: str = "open", events: list[str] | None = None,
                 error: BaseException | None = None) -> None:
        self.issue_state = issue_state
        self.events = events if events is not None else []
        self.error = error
        self.created: list[str] = []

    def resolve_repository(self, repository: str) -> str:
        return repository

    def search(self, repository: str, term: str):
        return [GitHubIssue(7, LINK["url"], "open", body) for body in self.created]

    def view(self, repository: str, number: int) -> GitHubIssue:
        self.events.append("sync")
        if self.error is not None:
            raise self.error
        return GitHubIssue(number, LINK["url"], self.issue_state, KEY_BODY)

    def create(self, repository: str, title: str, body: str, **_kwargs: object) -> None:
        self.events.append("create")
        self.created.append(body)


def _approval(**overrides: object) -> dict[str, object]:
    approval: dict[str, object] = {
        "key": KEY,
        "reviewed_brief_version": VERSION,
        "evidence_digest": EVIDENCE,
        "files": ["src/impl.py"],
        "actions": ["repair"],
        "call_limit": 100,
        "cost_cap_usd": "2.00",
        "runtime_minutes": 90,
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }
    approval.update(overrides)
    return approval


def _authority(revision: str = "r1", **overrides: object) -> dict[str, object]:
    return {"schema": 1, "revision": revision, "approvals": [_approval(**overrides)]}


def _config(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "enabled": True,
        "repository": REPOSITORY,
        "model": "orchestrator-model",
        "cost_cap_usd": "2.00",
        "authority": _authority(),
    }
    result.update(overrides)
    return result


def _args(state_data: dict | None, host: _FakeHost | None = None,
          **overrides: object) -> argparse.Namespace:
    events: list[str] = []
    values: dict[str, object] = {
        "command": "repair-cycle",
        "config": None,
        "config_data": _config(),
        "state": None,
        "state_data": state_data,
        "host": host or _FakeHost(events=events),
        "refresh": lambda: events.append("refresh"),
        "queue_client": _Queue(events=events),
        "repo_root": Path("."),
        "source": _Source(),
        "check": lambda issue, manifest: PASS,
        "now": NOW,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


BOUND = {"issue_id": ITEM, "key": KEY, "revision": "r1"}
LINK = {**BASE, "number": 7, "url": "https://example.invalid/issues/7", "state": "open"}


def _state() -> dict:
    return _revalidated_state(github_repair=dict(LINK))


def _events(args: argparse.Namespace) -> list[str]:
    return args.host.events


def test_parser_wires_one_shot_repair_cycle() -> None:
    parser = create_parser()
    args = parser.parse_args(["repair-cycle", "--config", "/etc/mending/repair-cycle.json"])

    assert args.command == "repair-cycle"
    assert args.config == "/etc/mending/repair-cycle.json"


def test_fresh_run_refreshes_publishes_and_dispatches_once(capsys) -> None:
    state = _state()
    args = _args(state)

    cmd_repair_cycle(args)

    assert _events(args) == ["refresh", "sync", "run"]
    recorded = state["repair_cycle"]
    assert recorded["authority"] == BOUND
    assert recorded["current_lease"]["window_start"] == "2026-09-13T00:00"
    assert (recorded["dispatch"]["phase"], recorded["dispatch"]["outcome"]) == (
        "returned",
        "completed",
    )
    assert recorded["dispatch"]["pull_requests"] == [PR_URL]
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (None, None)
    [request] = args.host.requests
    assert request.attempt_id == recorded["current_lease"]["attempt_id"]
    assert request.authorized_scope == f"repair {KEY}; files: src/impl.py"
    assert request.source_revision == "c" * 40
    assert "Parser duplicates the loader policy" in request.brief
    assert "Repair cycle active: pull request open." in capsys.readouterr().out


def test_selection_binds_authority_before_dispatch(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()))
    seen: list[dict] = []
    host = _FakeHost(on_run=lambda: seen.append(json.loads(state_path.read_text())))

    cmd_repair_cycle(_args(None, host, state=str(state_path)))

    persisted = seen[0]["repair_cycle"]
    assert persisted["authority"] == BOUND
    assert persisted["dispatch"]["phase"] == "intent"
    assert persisted["current_lease"]["attempt_id"] == host.requests[0].attempt_id
    assert seen[0]["work_items"][ITEM]["detail"]["github_repair"]["reviewed_brief_version"] == (
        VERSION
    )


def test_concurrent_invocations_dispatch_once(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()))
    started, release = threading.Event(), threading.Event()

    def block() -> None:
        started.set()
        assert release.wait(timeout=5)

    host = _FakeHost(on_run=block)

    def args() -> argparse.Namespace:
        return _args(None, host, state=str(state_path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cmd_repair_cycle, args())
        assert started.wait(timeout=5)
        second = executor.submit(cmd_repair_cycle, args())
        second.result(timeout=5)
        release.set()
        first.result(timeout=5)

    assert len(host.requests) == 1


def test_a_held_cycle_lock_leaves_state_untouched(tmp_path, capsys) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()))
    before = state_path.read_bytes()
    descriptor = os.open(tmp_path / "state.json.cycle.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        args = _args(None, state=str(state_path))
        cmd_repair_cycle(args)
    finally:
        os.close(descriptor)

    assert _events(args) == []
    assert state_path.read_bytes() == before
    assert "Repair cycle already running." in capsys.readouterr().out


@pytest.mark.parametrize(
    ("state", "overrides", "reason"),
    [
        ({"work_items": {}}, {}, "selected-repair-unavailable"),
        (_revalidated_state(github_repair={**LINK, "state": "closed"}),
         {"queue_client": _Queue("closed")}, "selected-repair-unavailable"),
        (None, {"config_data": _config(authority=None)}, "authority-missing"),
        (None, {"config_data": _config(authority=_authority(key="other"))}, "authority-missing"),
        # Sync drops evidence that no longer holds, so nothing is left to select.
        (None, {"check": lambda issue, manifest: replace(PASS, outcome="fail")},
         "selected-repair-unavailable"),
    ],
)
def test_discovery_only_runs_are_no_ops(state, overrides, reason, capsys) -> None:
    state = state or _state()
    args = _args(state, **overrides)

    cmd_repair_cycle(args)

    recorded = state["repair_cycle"]
    assert (recorded["current_lease"], recorded["parked_reason"]) == (None, None)
    assert args.host.requests == []
    assert f"Repair cycle no-op: {reason}." in capsys.readouterr().out


def test_missing_model_or_cost_cap_parks_without_external_calls() -> None:
    state = _state()
    args = _args(state, config_data=_config(model=None))

    cmd_repair_cycle(args)

    assert _events(args) == []
    assert state["repair_cycle"]["parked_reason"] == "missing-model"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"refresh": lambda: "refresh-failed"}, "refresh-failed"),
        ({"queue_client": _Queue(error=OSError("gh missing"))}, "publication-failed"),
        ({"queue_client": _Queue(error=TimeoutError("repair-cycle call bound exceeded"))},
         "publication-failed"),
    ],
)
def test_refresh_or_publication_failure_parks_before_selection(overrides, reason) -> None:
    state = _state()
    args = _args(state, **overrides)

    cmd_repair_cycle(args)

    assert args.host.requests == []
    assert state["repair_cycle"]["parked_reason"] == reason
    assert state["repair_cycle"]["current_lease"] is None


def test_publication_is_bounded_by_the_runtime(monkeypatch) -> None:
    bounds: list[float] = []

    def expire_immediately(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            bounds.append(seconds)
            repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_immediately)
    state = _state()
    args = _args(state)

    cmd_repair_cycle(args)

    assert bounds == [90 * 60]
    assert _events(args) == ["refresh"]
    assert state["repair_cycle"]["parked_reason"] == "publication-failed"


def test_file_backed_cycle_composes_refresh_sync_and_dispatch(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"work_items": {}}))

    def scan() -> None:
        written = json.loads(state_path.read_text())
        written["work_items"] = _revalidated_state()["work_items"]
        state_path.write_text(json.dumps(written))

    queue = _Queue()
    host = _FakeHost()
    cmd_repair_cycle(_args(None, host, state=str(state_path), refresh=scan, queue_client=queue))

    saved = json.loads(state_path.read_text())
    assert saved["work_items"][ITEM]["detail"]["github_repair"]["number"] == 7
    assert saved["repair_cycle"]["authority"] == BOUND
    assert saved["repair_cycle"]["current_lease"]["attempt_id"] == host.requests[0].attempt_id
    assert queue.events == ["create"]

    failing = tmp_path / "failing.json"
    failing.write_text(json.dumps(_state()))
    cmd_repair_cycle(_args(None, state=str(failing), refresh=lambda: "refresh-failed"))
    assert json.loads(failing.read_text())["repair_cycle"]["parked_reason"] == "refresh-failed"


@pytest.mark.parametrize(("returncode", "error", "reason"), [
    (0, None, None),
    (2, None, "refresh-failed"),
    (0, subprocess.TimeoutExpired("scan", 1), "refresh-failed"),
])
def test_production_refresh_scans_into_the_cycle_state_file(
    tmp_path, monkeypatch, returncode, error, reason
) -> None:
    calls: list[tuple[list[str], object, float]] = []

    def run(argv, *, cwd, timeout, check):
        calls.append((argv, cwd, timeout))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(repair_cycle.subprocess, "run", run)
    monkeypatch.setattr(repair_cycle, "_bring_current", lambda root, seconds: None)
    args = argparse.Namespace(state=str(tmp_path / "state.json"), repo_root=tmp_path)

    assert repair_cycle._refresh(args, CONFIG) == reason
    assert calls == [(
        [sys.executable, "-P", "-m", "desloppify", "scan", "--no-badge", "--state",
         str((tmp_path / "state.json").resolve())],
        tmp_path,
        90 * 60,
    )]


def test_refresh_parks_a_checkout_that_is_not_current_without_scanning(
    tmp_path, monkeypatch, capsys
) -> None:
    scans: list[object] = []
    monkeypatch.setattr(repair_cycle, "_bring_current", lambda root, seconds: "diverged")
    monkeypatch.setattr(repair_cycle.subprocess, "run", lambda *a, **k: scans.append(a))
    args = argparse.Namespace(state=str(tmp_path / "state.json"), repo_root=tmp_path)

    assert repair_cycle._refresh(args, CONFIG) == "checkout-not-current"
    assert scans == []
    assert "Repair cycle checkout not current: diverged" in capsys.readouterr().out


def _git(cwd: Path, *args: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                "-c", "commit.gpgsign=false")
    return subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(cwd: Path, name: str, text: str) -> str:
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", name)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path, monkeypatch) -> dict[str, Path]:
    """A bare remote on main, a publisher clone, and the cycle's checkout behind neither."""
    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))  # the developer's global git config stays out
    upstream, publisher = tmp_path / "upstream.git", tmp_path / "publisher"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    _git(tmp_path, "clone", "-q", str(upstream), str(publisher))
    _commit(publisher, "a.py", "a = 1\n")
    _git(publisher, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(upstream), str(tmp_path / "checkout"))
    return {"upstream": upstream, "publisher": publisher, "checkout": tmp_path / "checkout"}


def _publish(remote: dict[str, Path], name: str = "b.py") -> str:
    head = _commit(remote["publisher"], name, "b = 2\n")
    _git(remote["publisher"], "push", "-q", "origin", "main")
    return head


def test_a_clean_checkout_behind_its_remote_is_fast_forwarded(remote) -> None:
    checkout = remote["checkout"]
    hook = checkout / ".git" / "hooks" / "post-merge"
    hook.write_text(f"#!/bin/sh\ntouch {checkout.parent / 'hook-ran'}\n")
    hook.chmod(0o755)
    published = _publish(remote)

    assert repair_cycle._bring_current(checkout, 60) is None
    assert _git(checkout, "rev-parse", "HEAD") == published
    assert (checkout / "b.py").read_text() == "b = 2\n"
    assert not (checkout.parent / "hook-ran").exists()
    assert repair_cycle._bring_current(checkout, 60) is None  # already current


@pytest.mark.parametrize(("setup", "problem"), [
    ("dirty", "uncommitted changes"),
    ("diverged", "git merge failed: fatal: Not possible to fast-forward"),
    ("ahead", "ahead of origin"),
    ("other-branch", "on refs/heads/other, not refs/heads/main"),
    ("detached", "on HEAD, not refs/heads/main"),
])
def test_a_checkout_that_cannot_be_fast_forwarded_is_left_as_it_is(
    remote, setup, problem
) -> None:
    checkout = remote["checkout"]
    _publish(remote)
    if setup == "dirty":
        (checkout / "a.py").write_text("a = 'local work'\n")
    elif setup in {"diverged", "ahead"}:
        _commit(checkout, "local.py", "local = 1\n")
        if setup == "ahead":
            _git(checkout, "fetch", "-q")
            _git(checkout, "rebase", "-q", "origin/main")
    elif setup == "other-branch":
        _git(checkout, "checkout", "-q", "-b", "other")
    else:
        _git(checkout, "checkout", "-q", "--detach")
    before = (_git(checkout, "rev-parse", "HEAD"), _git(checkout, "status", "--porcelain"))

    assert repair_cycle._bring_current(checkout, 60).startswith(problem)
    assert (_git(checkout, "rev-parse", "HEAD"), _git(checkout, "status", "--porcelain")) == before
    if setup == "dirty":
        assert (checkout / "a.py").read_text() == "a = 'local work'\n"


def test_an_ignored_local_file_upstream_starts_tracking_is_kept(remote) -> None:
    publisher, checkout = remote["publisher"], remote["checkout"]
    _commit(publisher, ".gitignore", ".env\n")
    _git(publisher, "push", "-q", "origin", "main")
    assert repair_cycle._bring_current(checkout, 60) is None
    (checkout / ".env").write_text("operator secret\n")
    (publisher / ".env").write_text("upstream\n")
    _git(publisher, "add", "--force", ".env")
    _git(publisher, "commit", "-q", "-m", "track .env")
    _git(publisher, "push", "-q", "origin", "main")
    before = _git(checkout, "rev-parse", "HEAD")

    assert repair_cycle._bring_current(checkout, 60).startswith("git merge failed")
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert (checkout / ".env").read_text() == "operator secret\n"


def test_a_fast_forward_never_starts_near_the_deadline(remote, monkeypatch) -> None:
    checkout = remote["checkout"]
    before = _git(checkout, "rev-parse", "HEAD")
    _publish(remote)
    monkeypatch.setattr(repair_cycle, "_FAST_FORWARD_FLOOR_SECONDS", 61)

    assert repair_cycle._bring_current(checkout, 60) == "too little runtime left to fast-forward"
    assert _git(checkout, "rev-parse", "HEAD") == before


def test_the_remote_names_the_default_branch(remote) -> None:
    upstream, checkout = remote["upstream"], remote["checkout"]
    _git(remote["publisher"], "push", "-q", "origin", "main:trunk")
    _git(upstream, "symbolic-ref", "HEAD", "refs/heads/trunk")
    _publish(remote)  # main moves, but it is no longer the remote's default branch

    assert repair_cycle._bring_current(checkout, 60) == "on refs/heads/main, not refs/heads/trunk"


def test_an_unreachable_or_missing_remote_is_not_current(remote) -> None:
    checkout = remote["checkout"]
    _git(checkout, "remote", "set-url", "origin", str(checkout.parent / "missing.git"))
    assert repair_cycle._bring_current(checkout, 60) is not None
    _git(checkout, "remote", "remove", "origin")
    assert repair_cycle._bring_current(checkout, 60) is not None


def test_every_git_call_is_bounded_by_the_runtime(remote, monkeypatch) -> None:
    real_run = subprocess.run
    timeouts: list[float] = []

    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        return real_run(argv, **kwargs)

    _publish(remote)
    monkeypatch.setattr(repair_cycle.subprocess, "run", run)
    assert repair_cycle._bring_current(remote["checkout"], 60) is None
    assert timeouts and all(0 < timeout <= 60 for timeout in timeouts)

    def expire(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(repair_cycle.subprocess, "run", expire)
    assert repair_cycle._bring_current(remote["checkout"], 60) is not None


def test_the_refresh_scan_reads_the_fast_forwarded_checkout(remote, tmp_path, monkeypatch) -> None:
    checkout, real_run = remote["checkout"], subprocess.run
    published = _publish(remote)
    scanned: list[str] = []

    def run(argv, **kwargs):
        if argv[:3] == [sys.executable, "-P", "-m"]:
            scanned.append(_git(checkout, "rev-parse", "HEAD"))
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(repair_cycle.subprocess, "run", run)
    args = argparse.Namespace(state=str(tmp_path / "state.json"), repo_root=checkout)

    assert repair_cycle._refresh(args, CONFIG) is None
    assert scanned == [published]


def test_window_bounds_new_dispatches_and_a_closed_pr_settles() -> None:
    state = _state()
    host = _FakeHost()
    cmd_repair_cycle(_args(state, host))
    later = NOW + timedelta(hours=3)

    observed = _args(state, host, now=later)
    cmd_repair_cycle(observed)
    assert len(host.requests) == 1
    assert "refresh" not in _events(observed)

    host.prs = (_pr(open=False),)
    settled = _args(state, host, now=later)
    cmd_repair_cycle(settled)
    assert state["repair_cycle"]["dispatch"]["phase"] == "settled"
    assert state["repair_cycle"]["parked_reason"] == "window-attempt-complete"
    assert "refresh" not in _events(settled)
    assert len(host.requests) == 1

    host.prs = (_pr(),)
    cmd_repair_cycle(_args(state, host, now=NOW + timedelta(days=1)))
    assert len(host.requests) == 2
    assert state["repair_cycle"]["current_lease"]["window_start"] == "2026-09-14T00:00"
    assert [entry["dispatch"]["phase"] for entry in state["repair_cycle"]["attempt_history"]] == [
        "settled"
    ]


def test_configured_window_permits_a_second_dispatch_the_same_day() -> None:
    state = _state()
    host = _FakeHost(prs=(_pr(open=False),))
    config = _config(window_minutes=360)
    cmd_repair_cycle(_args(state, host, config_data=config))
    cmd_repair_cycle(_args(state, host, config_data=config, now=NOW + timedelta(hours=3)))

    assert len(host.requests) == 2
    assert state["repair_cycle"]["current_lease"]["window_start"] == "2026-09-13T12:00"


def test_an_open_pr_stays_active_without_exhausting_observation() -> None:
    state = _state()
    host = _FakeHost()
    config = _config(observation_call_limit=1)
    cmd_repair_cycle(_args(state, host, config_data=config))
    for day in range(1, 5):
        cmd_repair_cycle(_args(state, host, config_data=config, now=NOW + timedelta(days=day)))

    recorded = state["repair_cycle"]
    assert len(host.requests) == 1
    assert (recorded["attempt_failure"], recorded["observation_calls"]) == (None, 0)
    assert recorded["dispatch"]["phase"] == "returned"


@pytest.mark.parametrize(
    ("files", "approved", "failure"),
    [
        (("src/impl.py", "tests/test_impl.py"), ["src/impl.py", "tests/test_impl.py"], None),
        (("src/impl.py", "src/other.py"), ["src/impl.py"], "authority-scope-exceeded"),
    ],
)
def test_pull_request_edits_are_checked_against_the_approval(files, approved, failure) -> None:
    state = _state()
    host = _FakeHost(prs=(_pr(open=False, files=files),))

    cmd_repair_cycle(_args(state, host, config_data=_config(authority=_authority(files=approved))))

    recorded = state["repair_cycle"]
    assert recorded["attempt_failure"] == failure
    assert recorded["dispatch"]["phase"] == ("settled" if failure is None else "returned")


@pytest.mark.parametrize(
    ("config", "failure"),
    [
        (_config(authority=_authority(key="other")), "authority-revoked"),
        (_config(authority=None), "authority-revoked"),
        (_config(authority="yes"), "authority-invalid"),
    ],
)
def test_closed_pull_requests_without_a_readable_approval_fail_closed(config, failure) -> None:
    state = _state()
    _dispatched_attempt(state)
    host = _FakeHost(prs=(_pr(open=False, files=("src/impl.py", "src/unapproved.py")),))

    cmd_repair_cycle(_args(state, host, config_data=config))

    recorded = state["repair_cycle"]
    assert recorded["attempt_failure"] == failure
    assert recorded["dispatch"]["phase"] == "returned"


@pytest.mark.parametrize(
    ("outcome", "failure"),
    [
        (HostOutcome("failed", "host-error", cost_usd=Decimal("0.5"), calls=3), "host-failed"),
        (HostOutcome("stopped", "timeout", calls=3), "runtime-exhausted"),
        (HostOutcome("stopped", "call-limit", calls=101), "budget-exhausted"),
        (HostOutcome("unknown", "timeout-survivors", calls=2), "dispatch-outcome-unknown"),
    ],
)
def test_an_unfinished_host_run_stops_new_repairs(outcome, failure) -> None:
    state = _state()
    host = _FakeHost(outcome, found=HostReferences())

    cmd_repair_cycle(_args(state, host))
    assert state["repair_cycle"]["attempt_failure"] == failure

    cmd_repair_cycle(_args(state, host, now=NOW + timedelta(days=1)))
    assert len(host.requests) == 1
    assert state["repair_cycle"]["attempt_failure"] == failure
    assert CycleState.from_mapping(state["repair_cycle"]).reported_active is (
        outcome.state == "unknown"
    )


def test_completed_run_without_a_pull_request_fails() -> None:
    state = _state()
    cmd_repair_cycle(_args(state, _FakeHost(found=HostReferences())))

    assert state["repair_cycle"]["attempt_failure"] == "no-pull-request"


def test_a_failed_reference_lookup_parks_and_is_retried() -> None:
    state = _state()
    host = _FakeHost(found=HostLookupError("gh exited 1"))

    cmd_repair_cycle(_args(state, host))
    recorded = state["repair_cycle"]
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (
        None,
        "pull-request-lookup-unavailable",
    )

    host.found = HostReferences((PR_URL,))
    host.prs = (_pr(open=False),)
    cmd_repair_cycle(_args(state, host, now=NOW + timedelta(hours=1)))
    assert state["repair_cycle"]["dispatch"]["phase"] == "settled"
    assert state["repair_cycle"]["attempt_failure"] is None


def test_an_unreadable_pull_request_parks_without_settling() -> None:
    state = _state()
    host = _FakeHost(prs=HostLookupError("not a pull request of owner/repository"))

    cmd_repair_cycle(_args(state, host))

    recorded = state["repair_cycle"]
    assert recorded["parked_reason"] == "pull-request-lookup-unavailable"
    assert recorded["dispatch"]["phase"] == "returned"


def test_legacy_merge_fields_decode_and_authorize_nothing() -> None:
    state = _state()
    state["repair_cycle"] = {
        "current_lease": {
            "attempt_id": "legacy",
            "day_key": "2026-09-12",
            "deadline": (NOW - timedelta(days=1)).isoformat(),
            "call_limit": 100,
            "cost_cap_usd": "2.00",
            "merge_permit": True,
        },
        "authoritative_receipt": {"attempt_id": "legacy", "state": "terminal",
                                  "merge_consumed": True},
        "merge_permit_day": "2026-09-12",
    }
    host = _FakeHost()

    cmd_repair_cycle(_args(state, host))

    recorded = state["repair_cycle"]
    assert len(host.requests) == 1
    assert "merge_permit_day" not in recorded
    assert "merge_permit" not in recorded["current_lease"]
    assert recorded["attempt_history"][0]["lease"]["window_start"] == "2026-09-12T00:00"


def test_deadline_timer_interrupts_before_selection(monkeypatch) -> None:
    armed: list[float] = []

    def expire_at_selection(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            armed.append(seconds)
            if len(armed) == 2:  # the first bound is publication's
                repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_at_selection)
    state = _state()
    args = _args(state)

    cmd_repair_cycle(args)

    assert args.host.requests == []
    assert state["repair_cycle"]["parked_reason"] == "timeout"


def _recorded_attempt(state: dict, **config: object):
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(
        CycleConfig.from_mapping(_config(**config)), NOW, attempt_id="recorded-attempt"
    )
    assert lease is not None
    state["repair_cycle"] = cycle_state.to_mapping()
    return lease


def _dispatched_attempt(state: dict, *, bound: bool = True, outcome: str = "completed",
                        **config: object) -> None:
    lease = _recorded_attempt(state, **config)
    recorded = state["repair_cycle"]
    recorded["authority"] = BOUND if bound else None
    recorded["dispatch"] = DispatchRecord(
        lease.attempt_id, "returned", host_session_id(lease.attempt_id), REPOSITORY, "/repo",
        ("/repo",), outcome, (PR_URL,),
    ).to_mapping()


def test_an_interrupted_dispatch_is_observed_without_execution() -> None:
    state = _state()
    _recorded_attempt(state, runtime_minutes=1)
    cycle_state = CycleState.from_mapping(state["repair_cycle"])
    cycle_state.dispatch = DispatchRecord("recorded-attempt", "intent", repo_root="/repo")
    cycle_state.admit()
    state["repair_cycle"] = cycle_state.to_mapping()
    args = _args(state, now=NOW + timedelta(minutes=2))

    cmd_repair_cycle(args)

    assert _events(args) == []
    recorded = state["repair_cycle"]
    assert recorded["attempt_failure"] == "unsettled-reservation"
    assert recorded["observation_calls"] == 1


def test_a_replayed_dispatch_is_checked_against_the_approval() -> None:
    state = _state()
    _recorded_attempt(state)
    state["repair_cycle"]["authority"] = BOUND
    state["repair_cycle"]["dispatch"] = DispatchRecord("recorded-attempt", "unknown").to_mapping()
    host = _FakeHost(prs=(_pr(open=False, files=("src/other.py",)),))

    cmd_repair_cycle(_args(state, host))

    recorded = state["repair_cycle"]
    assert recorded["attempt_failure"] == "dispatch-outcome-unknown"
    assert recorded["parked_reason"] == "authority-scope-exceeded"
    assert host.pr_reads == [(PR_URL,)]


def test_disabled_configuration_still_observes_an_existing_attempt() -> None:
    state = _state()
    _dispatched_attempt(state)
    args = _args(state, config_data=_config(enabled=False))

    cmd_repair_cycle(args)

    assert _events(args) == []
    assert args.host.pr_reads == [(PR_URL,)]
    assert state["repair_cycle"]["attempt_failure"] is None


def test_observation_allowance_is_bounded_and_persisted(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state = _state()
    _dispatched_attempt(state)
    state_path.write_text(json.dumps(state))
    seen: list[int] = []
    host = _FakeHost(
        found=HostLookupError("gh offline"),
        on_references=lambda: seen.append(
            json.loads(state_path.read_text())["repair_cycle"]["observation_calls"]
        ),
    )

    def run() -> None:
        cmd_repair_cycle(_args(None, host, state=str(state_path),
                               config_data=_config(observation_call_limit=1),
                               now=NOW + timedelta(days=1)))

    run()
    first = json.loads(state_path.read_text())["repair_cycle"]
    assert (first["parked_reason"], first["attempt_failure"]) == (
        "pull-request-lookup-unavailable",
        None,
    )
    run()
    second = json.loads(state_path.read_text())["repair_cycle"]
    assert seen == [1]
    assert second["attempt_failure"] == "observation-exhausted"
    assert host.requests == []


def test_observation_is_time_bounded(monkeypatch) -> None:
    bounds: list[float] = []

    def expire_immediately(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            bounds.append(seconds)
            repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_immediately)
    state = _state()
    _dispatched_attempt(state)
    args = _args(state, config_data=_config(observation_minutes=2))

    cmd_repair_cycle(args)

    assert bounds == [120]
    assert args.host.lookups == []
    assert state["repair_cycle"]["parked_reason"] == "observation-timeout"
    assert state["repair_cycle"]["observation_calls"] == 1


def test_failed_attempt_requires_disposition_before_new_work() -> None:
    state = _state()
    _dispatched_attempt(state, outcome="failed")
    state["repair_cycle"]["dispatch"]["pull_requests"] = []
    state["repair_cycle"]["attempt_failure"] = "host-failed"
    host = _FakeHost(found=HostReferences())
    next_day = NOW + timedelta(days=1)

    cmd_repair_cycle(_args(state, host, now=next_day))
    assert state["repair_cycle"]["dispatch"]["phase"] == "settled"
    assert state["repair_cycle"]["parked_reason"] == "disposition-required"

    with pytest.raises(CommandError, match="not the current attempt"):
        cmd_repair_cycle(_args(state, host, now=next_day, dispose_attempt="other"))
    cmd_repair_cycle(_args(state, host, now=next_day, dispose_attempt="recorded-attempt"))
    assert host.requests == []

    cmd_repair_cycle(_args(state, host, now=next_day))
    recorded = state["repair_cycle"]
    assert len(host.requests) == 1
    assert recorded["current_lease"]["attempt_id"] != "recorded-attempt"
    assert (recorded["disposed_attempt"], recorded["attempt_failure"]) == (None, "no-pull-request")


def test_disposition_requires_a_recorded_failure() -> None:
    state = _state()
    _recorded_attempt(state)

    with pytest.raises(CommandError, match="no recorded failure"):
        cmd_repair_cycle(_args(state, dispose_attempt="recorded-attempt"))
    assert state["repair_cycle"]["disposed_attempt"] is None


def test_parser_wires_dispose_attempt() -> None:
    args = create_parser().parse_args(
        ["repair-cycle", "--config", "/etc/mending/repair-cycle.json", "--dispose-attempt", "a1"]
    )

    assert args.dispose_attempt == "a1"
    assert args.confirm_stopped is False
    confirmed = create_parser().parse_args(
        ["repair-cycle", "--config", "c.json", "--dispose-attempt", "a1", "--confirm-stopped"]
    )
    assert confirmed.confirm_stopped is True


def test_config_host_keys_default_and_decode() -> None:
    defaults = CycleConfig.from_mapping(_config())
    assert (defaults.host_executable, defaults.adept_skills_dir, defaults.adept_skills_version) == (
        "claude",
        None,
        None,
    )
    configured = CycleConfig.from_mapping(
        _config(
            host_executable=" /usr/local/bin/claude ",
            adept_skills_dir="/opt/adept",
            adept_skills_version="7.2.0",
        )
    )
    assert configured.host_executable == "/usr/local/bin/claude"
    assert configured.adept_skills_dir == "/opt/adept"
    assert configured.adept_skills_version == "7.2.0"
    with pytest.raises(ValueError, match="host_executable"):
        CycleConfig.from_mapping(_config(host_executable=""))


def test_config_observation_keys_default_and_decode() -> None:
    defaults = CycleConfig.from_mapping(_config())
    assert (defaults.observation_call_limit, defaults.observation_seconds) == (3, 300)
    configured = CycleConfig.from_mapping(_config(observation_call_limit=5, observation_minutes=2))
    assert (configured.observation_call_limit, configured.observation_seconds) == (5, 120)
    with pytest.raises(ValueError, match="observation_call_limit"):
        CycleConfig.from_mapping(_config(observation_call_limit=0))
    with pytest.raises(ValueError, match="observation_minutes"):
        CycleConfig.from_mapping(_config(observation_minutes=True))


def test_legacy_state_decodes_and_new_fields_validate() -> None:
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(CycleConfig.from_mapping(_config()), NOW, attempt_id="legacy")
    assert lease is not None
    legacy = {
        key: value
        for key, value in cycle_state.to_mapping().items()
        if key
        not in {
            "observation_calls",
            "attempt_failure",
            "disposed_attempt",
            "consumed_cost_usd",
            "consumed_calls",
            "reserved_cost_usd",
            "reserved_calls",
            "authority",
        }
    }

    decoded = CycleState.from_mapping(legacy)
    assert decoded.authority is None
    for malformed in ({"issue_id": ITEM, "key": KEY}, {**BOUND, "revision": 1}, "bound"):
        assert CycleState.from_mapping({**legacy, "authority": malformed}).authority is None

    assert decoded.current_lease == lease
    assert (decoded.observation_calls, decoded.attempt_failure, decoded.disposed_attempt) == (
        0,
        None,
        None,
    )
    assert (decoded.consumed_cost_usd, decoded.consumed_calls) == (Decimal(0), 0)
    assert (decoded.reserved_cost_usd, decoded.reserved_calls) == (Decimal(0), 0)
    for field, bad in (
        ("observation_calls", -1),
        ("consumed_cost_usd", "-0.01"),
        ("consumed_cost_usd", True),
        ("reserved_cost_usd", "NaN"),
        ("reserved_cost_usd", "lots"),
        ("consumed_calls", -1),
        ("reserved_calls", True),
        ("reserved_calls", "2"),
        ("observation_calls", True),
        ("attempt_failure", 3),
        ("disposed_attempt", ""),
    ):
        with pytest.raises(ValueError):
            CycleState.from_mapping({**legacy, field: bad})


def test_dispatch_record_round_trips_and_legacy_lease_is_unknown() -> None:
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(CycleConfig.from_mapping(_config()), NOW, attempt_id="a1")
    assert lease is not None
    cycle_state.dispatch = DispatchRecord(
        "a1", "returned", "s", "o/r", "/repo", ("/repo",), "completed",
        ("https://x/pull/1",), (), ("/w",), NOW + timedelta(minutes=30),
    )
    assert CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping()))) == cycle_state
    no_deadline = {key: value for key, value in cycle_state.to_mapping()["dispatch"].items()
                   if key != "deadline"}
    assert DispatchRecord.from_mapping(no_deadline).deadline is None
    record = DispatchRecord("a1", "returned", pull_requests=("p2", "p0"))
    assert record.with_references(("p1", "p2"), ("i1",), ()).pull_requests == ("p2", "p0", "p1")
    assert record.with_references(("p1", "p2"), ("i1",), ()).issues == ("i1",)

    legacy = {key: value for key, value in cycle_state.to_mapping().items()
              if key not in {"dispatch", "attempt_history"}}
    decoded = CycleState.from_mapping(legacy)
    assert decoded.current_lease == lease
    assert decoded.dispatch == DispatchRecord("a1", "unknown")
    assert decoded.attempt_history == []
    assert CycleState.from_mapping({}).dispatch is None

    valid = cycle_state.to_mapping()
    dispatch = valid["dispatch"]
    assert isinstance(dispatch, dict)
    for bad in (
        {**valid, "current_lease": None},
        {**valid, "dispatch": {**dispatch, "attempt_id": "other"}},
        {**valid, "dispatch": {**dispatch, "phase": "other"}},
        {**valid, "dispatch": {**dispatch, "pull_requests": "p1"}},
        {**valid, "dispatch": {**dispatch, "worktrees": [""]}},
        {**valid, "dispatch": {**dispatch, "deadline": "soon"}},
        {**valid, "dispatch": {**dispatch, "deadline": "2026-09-13T09:30:00"}},
        {**valid, "dispatch": "a1"},
        {**valid, "attempt_history": {}},
        {**valid, "attempt_history": [{"lease": None}]},
        {**valid, "attempt_history": [{"lease": lease.to_mapping(), "dispatch": {"phase": "x"}}]},
    ):
        with pytest.raises(ValueError):
            CycleState.from_mapping(bad)


def test_begin_archives_replaced_attempt() -> None:
    config = CycleConfig.from_mapping(_config())
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(config, NOW, attempt_id="a1")
    assert lease is not None
    cycle_state.dispatch = DispatchRecord("a1", "returned", "s", outcome="failed")
    cycle_state.consumed_calls = 4
    cycle_state.fail("host-error")
    cycle_state.dispose("a1")

    assert cycle_state.begin(config, NOW + timedelta(days=1), attempt_id="a2") is not None
    assert cycle_state.begin(config, NOW + timedelta(days=1), attempt_id="a3") is None

    assert cycle_state.dispatch is None
    assert cycle_state.attempt_history == [
        {
            "lease": lease.to_mapping(),
            "authoritative_receipt": None,
            "parked_reason": "host-error",
            "attempt_failure": "host-error",
            "disposed": True,
            "observation_calls": 0,
            "consumed_cost_usd": "0",
            "consumed_calls": 4,
            "reserved_cost_usd": "0",
            "reserved_calls": 0,
            "dispatch": DispatchRecord("a1", "returned", "s", outcome="failed").to_mapping(),
            "authority": None,
        }
    ]
    round_trip = CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping())))
    assert round_trip.attempt_history == cycle_state.attempt_history


CONFIG = CycleConfig.from_mapping(_config())


def _budget_state() -> CycleState:
    cycle_state = CycleState.empty()
    cycle_state.begin(CycleConfig.from_mapping(_config(call_limit=10)), NOW, attempt_id="a1")
    return cycle_state


def test_budget_admission_reserves_remainder_and_settles() -> None:
    cycle_state = _budget_state()

    admission = cycle_state.admit()

    assert admission == BudgetAdmission(Decimal("2.00"), 10)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal("2.00"), 10)
    assert cycle_state.admit() is None
    cycle_state.settle(admission, Decimal("0.75"), 3)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal(0), 0)
    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal("0.75"), 3)
    assert not cycle_state.budget_exceeded
    assert cycle_state.admit() == BudgetAdmission(Decimal("1.25"), 7)
    round_trip = CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping())))
    assert round_trip == cycle_state
    cycle_state.fail("budget-exhausted")
    cycle_state.dispose("a1")
    assert cycle_state.begin(CycleConfig.from_mapping(_config()), NOW + timedelta(days=1))
    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal(0), 0)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal(0), 0)
    with pytest.raises(ValueError, match="no current lease"):
        CycleState.empty().admit()


def test_unmeasured_cost_charges_whole_admission() -> None:
    cycle_state = _budget_state()
    admission = cycle_state.admit()
    assert admission is not None

    cycle_state.settle(admission, None, 11)

    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal("2.00"), 11)
    assert cycle_state.budget_exceeded
    assert cycle_state.admit() is None


def _leased(state: dict) -> CycleState:
    cycle_state = _budget_state()
    cycle_state.authority = BOUND
    state.update(_state())
    state["repair_cycle"] = cycle_state.to_mapping()
    return cycle_state


@pytest.mark.parametrize(
    ("outcome", "consumed", "failure", "parked"),
    [
        (HostOutcome("completed", cost_usd=Decimal("0.5"), calls=3), ("0.5", 3), None, None),
        (
            HostOutcome("failed", "host-error", cost_usd=Decimal("2.5"), calls=4),
            ("2.5", 4),
            "budget-exhausted",
            "budget-exhausted",
        ),
        (HostOutcome("parked", "unenforceable-limit"), ("0", 0), None, "unenforceable-limit"),
        (
            HostOutcome("stopped", "call-limit", calls=11),
            ("2.00", 11),
            "budget-exhausted",
            "budget-exhausted",
        ),
        (
            HostOutcome("unknown", "timeout-survivors", calls=2),
            ("2.00", 10),
            "dispatch-outcome-unknown",
            "dispatch-outcome-unknown",
        ),
    ],
)
def test_dispatch_budget_outcomes(outcome, consumed, failure, parked) -> None:
    state = _state()
    cycle_state = _leased(state)
    host = _FakeHost(outcome)

    assert _dispatch_host(_args(state, host), state, cycle_state, CONFIG, host) == outcome

    assert host.admissions == [BudgetAdmission(Decimal("2.00"), 10)]
    recorded = state["repair_cycle"]
    assert (Decimal(recorded["consumed_cost_usd"]), recorded["consumed_calls"]) == (
        Decimal(consumed[0]),
        consumed[1],
    )
    assert (Decimal(recorded["reserved_cost_usd"]), recorded["reserved_calls"]) == (0, 0)
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (failure, parked)


def test_dispatch_refuses_without_admission() -> None:
    state = _state()
    cycle_state = _leased(state)
    host = _FakeHost()
    lease = cycle_state.current_lease
    assert lease is not None
    late = _args(state, host, now=lease.deadline)
    assert _dispatch_host(late, state, cycle_state, CONFIG, host) is None
    assert state["repair_cycle"]["attempt_failure"] == "runtime-exhausted"
    assert cycle_state.reserved_calls == 0

    state = _state()
    cycle_state = _leased(state)
    cycle_state.consumed_calls = 10
    assert _dispatch_host(_args(state, host), state, cycle_state, CONFIG, host) is None
    assert state["repair_cycle"]["attempt_failure"] == "budget-exhausted"
    assert host.admissions == []

    with pytest.raises(ValueError, match="no current lease"):
        _dispatch_host(_args(state, host), state, CycleState.empty(), CONFIG, host)


def test_dispatch_persists_reservation_before_launch(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    seen: list[dict] = []

    def read_disk() -> None:
        seen.append(json.loads(state_path.read_text())["repair_cycle"])

    host = _FakeHost(on_run=read_disk)
    args = _args(None, host, state=str(state_path))
    with repair_cycle._locked_state(args) as state:
        cycle_state = _leased(state)
        _dispatch_host(args, state, cycle_state, CONFIG, host)

    assert (seen[0]["reserved_cost_usd"], seen[0]["reserved_calls"]) == ("2.00", 10)
    assert seen[0]["dispatch"] == DispatchRecord(
        "a1", "intent", host_session_id("a1"), REPOSITORY, str(Path(".").resolve()), ("/repo",),
        deadline=host.requests[0].deadline,
    ).to_mapping()
    settled = json.loads(state_path.read_text())["repair_cycle"]
    assert (settled["reserved_calls"], settled["consumed_calls"]) == (0, 3)
    assert settled["consumed_cost_usd"] == "0.5"


def test_interrupted_dispatch_keeps_reservation(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    interrupted = _FakeHost(error=KeyboardInterrupt())
    args = _args(None, interrupted, state=str(state_path))

    with pytest.raises(KeyboardInterrupt), repair_cycle._locked_state(args) as state:
        cycle_state = _leased(state)
        deadline = cycle_state.current_lease.deadline  # type: ignore[union-attr]
        _dispatch_host(args, state, cycle_state, CONFIG, interrupted)

    host = _FakeHost()
    after_deadline = _args(None, host, state=str(state_path), now=deadline)
    with repair_cycle._locked_state(after_deadline) as state:
        restarted = repair_cycle._cycle_state(state)
        assert (restarted.reserved_cost_usd, restarted.reserved_calls) == (Decimal("2.00"), 10)
        restarted.dispatch = None
        assert _dispatch_host(after_deadline, state, restarted, CONFIG, host) is None

    assert host.admissions == []
    recorded = json.loads(state_path.read_text())["repair_cycle"]
    assert recorded["attempt_failure"] == "unsettled-reservation"
    assert recorded["reserved_calls"] == 10


def test_returned_dispatch_is_persisted_before_reference_lookup(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    seen: list[dict] = []
    host = _FakeHost(
        on_references=lambda: seen.append(json.loads(state_path.read_text())["repair_cycle"])
    )
    args = _args(None, host, state=str(state_path))

    with repair_cycle._locked_state(args) as state:
        cycle_state = _leased(state)
        _dispatch_host(args, state, cycle_state, CONFIG, host)

    assert seen[0]["dispatch"]["phase"] == "returned"
    assert seen[0]["dispatch"]["outcome"] == "completed"
    assert (seen[0]["reserved_calls"], seen[0]["consumed_calls"]) == (0, 3)
    assert host.lookups == [("a1", REPOSITORY, Path(".").resolve(), ("/repo",))]
    recorded = json.loads(state_path.read_text())["repair_cycle"]["dispatch"]
    assert recorded["pull_requests"] == [PR_URL]


def test_dispatch_record_follows_the_outcome() -> None:
    state = _state()
    cycle_state = _leased(state)
    parked = _FakeHost(HostOutcome("parked", "missing-host"))
    assert _dispatch_host(_args(state, parked), state, cycle_state, CONFIG, parked)
    assert cycle_state.dispatch is None
    assert parked.lookups == []

    state = _state()
    cycle_state = _leased(state)
    offline = _FakeHost(found=HostLookupError("gh exited 1"))
    assert _dispatch_host(_args(state, offline), state, cycle_state, CONFIG, offline)
    assert cycle_state.dispatch is not None
    assert (cycle_state.dispatch.phase, cycle_state.dispatch.pull_requests) == ("returned", ())
    assert state["repair_cycle"]["parked_reason"] == "pull-request-lookup-unavailable"
    assert state["repair_cycle"]["attempt_failure"] is None

    state = _state()
    cycle_state = _leased(state)
    no_git = _FakeHost(snapshot=HostLookupError("git exited 128"))
    assert _dispatch_host(_args(state, no_git), state, cycle_state, CONFIG, no_git) is None
    assert no_git.admissions == []
    assert (cycle_state.dispatch, cycle_state.reserved_calls) == (None, 0)
    assert state["repair_cycle"]["parked_reason"] == "dispatch-lookup-unavailable"


@pytest.mark.parametrize(
    ("phase", "reserved", "alive", "found", "failure", "parked"),
    [
        ("intent", True, True, None, None, "dispatch-in-flight"),
        ("intent", True, None, None, None, "dispatch-unverified"),
        ("intent", True, False, None, "unsettled-reservation", "unsettled-reservation"),
        ("intent", True, False, "error", "unsettled-reservation", "unsettled-reservation"),
        ("unknown", False, False, None, "dispatch-outcome-unknown", "dispatch-outcome-unknown"),
        ("unknown", False, False, "error", "dispatch-outcome-unknown", "dispatch-outcome-unknown"),
        ("returned", False, False, "error", None, "dispatch-lookup-unavailable"),
        ("returned", False, False, None, None, "already-dispatched"),
    ],
)
def test_replay_never_relaunches(phase, reserved, alive, found, failure, parked) -> None:
    state = _state()
    cycle_state = _leased(state)
    recorded = DispatchRecord(
        "a1", phase, pull_requests=("https://example.invalid/pull/0",),
        **({} if phase == "unknown" else {
            "session_id": host_session_id("a1"), "repository": "owner/recorded",
            "repo_root": "/recorded", "prior_worktrees": ("/recorded",),
        }),
    )
    cycle_state.dispatch = recorded
    if reserved:
        cycle_state.admit()
    lookup = HostLookupError("gh exited 1") if found == "error" else HostReferences(
        ("https://example.invalid/pull/1",), ("https://example.invalid/issues/2",), ("/wt",)
    )
    host = _FakeHost(alive=alive, found=lookup)
    # A dead or unverifiable worker is observed the same way after the lease deadline.
    later = timedelta(minutes=1) if alive else timedelta(days=1)

    assert _dispatch_host(_args(state, host, now=NOW + later), state, cycle_state, CONFIG,
                          host) is None

    assert (host.admissions, host.stopped) == ([], [])
    assert (state["repair_cycle"]["attempt_failure"], state["repair_cycle"]["parked_reason"]) == (
        failure,
        parked,
    )
    if alive is not False:
        assert host.lookups == []
        return
    expected = ("a1", REPOSITORY, Path("."), ()) if phase == "unknown" else (
        "a1", "owner/recorded", Path("/recorded"), ("/recorded",)
    )
    assert host.lookups == [expected]
    record = state["repair_cycle"]["dispatch"]
    assert record["pull_requests"][0] == "https://example.invalid/pull/0"
    assert len(record["pull_requests"]) == (1 if found == "error" else 2)
    assert record["worktrees"] == ([] if phase == "unknown" or found == "error" else ["/wt"])


@pytest.mark.parametrize(
    ("recorded_deadline", "after", "stops", "failure", "parked"),
    [
        # The approval expired before the lease: the reason a live run would give.
        (NOW + timedelta(minutes=30), timedelta(minutes=30), True,
         "authority-expired", "authority-expired"),
        (NOW + timedelta(minutes=30), timedelta(minutes=29), True, None, "dispatch-in-flight"),
        # A record from before run deadlines were recorded is held to its lease.
        (None, timedelta(minutes=89), True, None, "dispatch-in-flight"),
        (None, timedelta(minutes=90), True, "runtime-exhausted", "runtime-exhausted"),
        # A recorded deadline never extends the lease.
        (NOW + timedelta(days=1), timedelta(minutes=90), True,
         "runtime-exhausted", "runtime-exhausted"),
        # A tree that cannot be verified empty is not reported stopped.
        (None, timedelta(days=1), False, "dispatch-outcome-unknown", "dispatch-outcome-unknown"),
    ],
    ids=["approval-expiry", "before-expiry", "legacy-before-lease", "legacy-at-lease",
         "never-extends-lease", "unverified-stop"],
)
def test_replay_stops_a_worker_alive_past_its_run_deadline(
    recorded_deadline, after, stops, failure, parked
) -> None:
    state = _state()
    cycle_state = _leased(state)
    cycle_state.dispatch = DispatchRecord("a1", "intent", deadline=recorded_deadline)
    cycle_state.admit()
    host = _FakeHost(alive=True, stops=stops, found=HostReferences((PR_URL,)))

    assert _dispatch_host(_args(state, host, now=NOW + after), state, cycle_state, CONFIG,
                          host) is None

    recorded = state["repair_cycle"]
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (failure, parked)
    assert host.stopped == ([] if parked == "dispatch-in-flight" else ["a1"])
    assert host.admissions == []
    stopped = failure in {"authority-expired", "runtime-exhausted"}
    # References are read only once the worker is verified stopped.
    assert len(host.lookups) == int(stopped)
    # A verified stop is recorded and charged as the live run's stop would be.
    expected = ("returned", "stopped", 0, 10) if stopped else ("intent", None, 10, 0)
    dispatch = recorded["dispatch"]
    assert (dispatch["phase"], dispatch["outcome"], recorded["reserved_calls"],
            recorded["consumed_calls"]) == expected


def test_an_exhausted_observation_allowance_still_stops_an_expired_worker() -> None:
    state = _state()
    cycle_state = _leased(state)
    cycle_state.dispatch = DispatchRecord("a1", "intent", deadline=NOW + timedelta(minutes=30))
    cycle_state.observation_calls = 3
    state["repair_cycle"] = cycle_state.to_mapping()
    host = _FakeHost(alive=True)

    cmd_repair_cycle(_args(state, host, now=NOW + timedelta(hours=1)))

    recorded = state["repair_cycle"]
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (
        "observation-exhausted", "authority-expired"
    )
    assert (host.stopped, host.lookups, host.events) == (["a1"], [], [])


def _binding(**overrides: object) -> AuthorityBinding:
    values: dict[str, object] = {
        "repository": REPOSITORY,
        "key": KEY,
        "reviewed_brief_version": VERSION,
        "evidence_digest": EVIDENCE,
        "files": frozenset({"src/impl.py"}),
        "action": "repair",
        "call_limit": 100,
        "cost_cap_usd": Decimal("2.00"),
        "runtime_seconds": 90 * 60,
    }
    values.update(overrides)
    return AuthorityBinding(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("authority", "binding", "bound", "reason"),
    [
        (_authority(), {}, False, None),
        (_authority("r2"), {}, True, None),
        (None, {}, False, "authority-missing"),
        (_authority(key="other"), {}, False, "authority-missing"),
        (None, {}, True, "authority-revoked"),
        (_authority(key="other"), {}, True, "authority-revoked"),
        (_authority(revoked=True), {}, False, "authority-revoked"),
        (_authority(), {"repository": "other/repository"}, False, "authority-mismatch"),
        (_authority(), {"reviewed_brief_version": "f" * 64}, False, "authority-mismatch"),
        (_authority(), {"evidence_digest": "f" * 64}, False, "authority-mismatch"),
        (_authority(expires_at=NOW.isoformat()), {}, False, "authority-expired"),
        (_authority(), {"action": "merge"}, False, "authority-scope-exceeded"),
        (_authority(), {"files": frozenset({"src/impl.py", "x.py"})}, False,
         "authority-scope-exceeded"),
        (_authority(), {"call_limit": 101}, False, "authority-limits-exceeded"),
        (_authority(), {"cost_cap_usd": Decimal("2.01")}, False, "authority-limits-exceeded"),
        (_authority(), {"runtime_seconds": 90 * 60 + 1}, False, "authority-limits-exceeded"),
    ],
)
def test_check_authority_reasons(authority, binding, bound, reason) -> None:
    decoded = authority_from_mapping(REPOSITORY, authority)

    assert check_authority(decoded, _binding(**binding), NOW, bound) == reason


def test_authority_decoding() -> None:
    assert authority_from_mapping(REPOSITORY, None) is None
    with pytest.raises(UnsupportedAuthority):
        authority_from_mapping(REPOSITORY, {**_authority(), "schema": 2})
    with pytest.raises(UnsupportedAuthority):
        authority_from_mapping(REPOSITORY, _authority(actions=["repair", "merge"]))
    duplicate = {**_authority(), "approvals": [_approval(), _approval()]}
    for bad in (
        "authority",
        {**_authority(), "approvals": {}},
        {**_authority(), "revision": ""},
        duplicate,
        _authority(expires_at="2026-09-13T09:00:00"),
        _authority(expires_at="soon"),
        _authority(files=[]),
        _authority(call_limit=True),
        _authority(cost_cap_usd="0"),
        _authority(cost_cap_usd=1.5),
        _authority(revoked="no"),
    ):
        with pytest.raises(ValueError):
            authority_from_mapping(REPOSITORY, bad)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"config_data": _config(authority="yes")}, "authority-invalid"),
        ({"config_data": _config(authority={**_authority(), "schema": 2})},
         "authority-unsupported"),
        ({"config_data": _config(authority=_authority(reviewed_brief_version="f" * 64))},
         "authority-mismatch"),
        ({"config_data": _config(authority=_authority(revoked=True))}, "authority-revoked"),
        ({"config_data": _config(authority=_authority(expires_at=NOW.isoformat()))},
         "authority-expired"),
        ({"config_data": _config(authority=_authority(files=["src/other.py"]))},
         "authority-scope-exceeded"),
        ({"config_data": _config(call_limit=500)}, "authority-limits-exceeded"),
        ({"source": _Source(AnalysisUnknown("git unavailable"))}, "source-unreadable"),
    ],
)
def test_selection_parks_without_authority(overrides, reason) -> None:
    state = _state()
    args = _args(state, **overrides)

    cmd_repair_cycle(args)

    assert args.host.requests == []
    assert state["repair_cycle"]["parked_reason"] == reason
    assert state["repair_cycle"]["authority"] is None


@pytest.mark.parametrize(
    ("foreign_owner", "writable", "reason", "dispatches"),
    [
        (False, set(), "authority-untrusted", 0),
        (True, {"file"}, "authority-untrusted", 0),
        (True, {"parent"}, "authority-untrusted", 0),
        (True, {"ancestor"}, "authority-untrusted", 0),
        (True, set(), None, 1),
    ],
)
def test_only_a_foreign_read_only_config_grants_authority(
    tmp_path, monkeypatch, foreign_owner, writable, reason, dispatches
) -> None:
    path = (tmp_path / "etc" / "repair-cycle.json").resolve()
    path.parent.mkdir()
    path.write_text(json.dumps(_config()))
    named = {"file": path, "parent": path.parent, "ancestor": path.parent.parent}
    writable_paths = {named[name] for name in writable}
    if foreign_owner:
        monkeypatch.setattr(repair_cycle.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(repair_cycle.os, "access", lambda entry, _mode: entry in writable_paths)
    state = _state()
    args = _args(state, config=str(path), config_data=None)

    cmd_repair_cycle(args)

    assert len(args.host.requests) == dispatches
    assert state["repair_cycle"]["parked_reason"] == reason


def test_missing_or_symlinked_config_is_untrusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(repair_cycle.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(repair_cycle.os, "access", lambda _entry, _mode: False)
    target = (tmp_path / "repair-cycle.json").resolve()
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    def trusted(path: Path) -> bool:
        return repair_cycle._trusted_config(argparse.Namespace(config=str(path), config_data=None))

    (tmp_path / "sub").mkdir()
    assert trusted(target) is True
    assert trusted(link) is False
    assert trusted(tmp_path / "sub" / ".." / "repair-cycle.json") is False
    assert trusted(tmp_path / "gone.json") is False


def test_dispatch_authority_check_is_time_bounded(monkeypatch) -> None:
    state = _state()
    cycle_state = _leased(state)
    host = _FakeHost()

    def overdue(*_args, **_kwargs):
        raise TimeoutError("repair-cycle call bound exceeded")

    monkeypatch.setattr(repair_cycle, "source_comparison", overdue)
    assert _dispatch_host(_args(state, host), state, cycle_state, CONFIG, host) is None

    assert host.admissions == []
    assert state["repair_cycle"]["parked_reason"] == "timeout"


@pytest.mark.parametrize(
    ("bound", "config", "reason"),
    [
        (False, _config(), "authority-missing"),
        (True, _config(authority=_authority(revoked=True)), "authority-revoked"),
        (True, _config(authority=None), "authority-revoked"),
        (True, _config(authority=_authority(key="other")), "authority-revoked"),
        (True, _config(authority=_authority(expires_at=NOW.isoformat())), "authority-expired"),
        (True, _config(cost_cap_usd="1.00", authority=_authority(cost_cap_usd="1.00")),
         "authority-limits-exceeded"),
    ],
)
def test_dispatch_rechecks_authority(bound, config, reason) -> None:
    state = _state()
    cycle_state = _leased(state)
    if not bound:
        cycle_state.authority = None
    host = _FakeHost()

    result = _dispatch_host(
        _args(state, host), state, cycle_state, CycleConfig.from_mapping(config), host
    )

    assert result is None
    assert (host.admissions, host.lookups, cycle_state.dispatch) == ([], [], None)
    assert state["repair_cycle"]["parked_reason"] == reason
    assert state["repair_cycle"]["reserved_calls"] == 0


def test_dispatch_rechecks_source() -> None:
    state = _state()
    cycle_state = _leased(state)
    host = _FakeHost()
    stale = _args(state, host, source=_Source(AnalysisUnknown("git unavailable")))

    assert _dispatch_host(stale, state, cycle_state, CONFIG, host) is None

    assert host.admissions == []
    assert state["repair_cycle"]["parked_reason"] == "source-unreadable"


def test_dispatch_request_follows_the_dispatch_time_binding() -> None:
    state = _state()
    cycle_state = _leased(state)
    state["work_items"][ITEM]["detail"]["verification"] = "Run a different suite"
    host = _FakeHost()

    assert _dispatch_host(_args(state, host), state, cycle_state, CONFIG, host) is None

    assert host.requests == []
    assert state["repair_cycle"]["parked_reason"] == "authority-mismatch"


@pytest.mark.parametrize(
    ("config", "failure"),
    [
        (_config(), None),
        (_config(authority=_authority("r2")), None),
        (_config(authority=_authority(revoked=True)), "authority-revoked"),
        (_config(authority=_authority(key="other")), "authority-revoked"),
    ],
)
def test_resume_rechecks_an_active_attempt(config, failure) -> None:
    state = _state()
    _dispatched_attempt(state)
    args = _args(state, config_data=config)

    cmd_repair_cycle(args)

    recorded = state["repair_cycle"]
    assert args.host.requests == []
    assert args.host.pr_reads == [(PR_URL,)]
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (failure, failure)


def test_resume_of_a_changed_brief_fails_the_attempt() -> None:
    state = _state()
    _dispatched_attempt(state)
    state["work_items"][ITEM]["detail"]["verification"] = "Run a different suite"

    cmd_repair_cycle(_args(state))

    assert state["repair_cycle"]["attempt_failure"] == "authority-mismatch"


def test_dispose_refuses_an_attempt_last_reported_active() -> None:
    state = _state()
    _dispatched_attempt(state)
    cmd_repair_cycle(_args(state, config_data=_config(authority=_authority(revoked=True))))
    assert state["repair_cycle"]["attempt_failure"] == "authority-revoked"

    with pytest.raises(CommandError, match="--confirm-stopped"):
        cmd_repair_cycle(_args(state, dispose_attempt="recorded-attempt"))
    assert state["repair_cycle"]["disposed_attempt"] is None

    cmd_repair_cycle(_args(state, dispose_attempt="recorded-attempt", confirm_stopped=True))
    assert state["repair_cycle"]["disposed_attempt"] == "recorded-attempt"


@pytest.mark.parametrize(
    "dispatch",
    [
        DispatchRecord("a1", "intent"),
        DispatchRecord("a1", "returned", outcome="unknown"),
        DispatchRecord("a1", "returned", outcome="completed"),
        DispatchRecord("a1", "returned", outcome="failed", pull_requests=(PR_URL,)),
    ],
)
def test_dispose_refuses_an_unsettled_dispatch(dispatch) -> None:
    cycle_state = _budget_state()
    cycle_state.dispatch = dispatch
    cycle_state.fail("host-error")

    with pytest.raises(ValueError, match="--confirm-stopped"):
        cycle_state.dispose("a1")
    cycle_state.dispose("a1", confirm_stopped=True)
    assert cycle_state.disposed


def test_resume_of_an_unbound_active_attempt_fails() -> None:
    state = _state()
    _dispatched_attempt(state, bound=False)

    cmd_repair_cycle(_args(state))

    assert state["repair_cycle"]["attempt_failure"] == "authority-missing"


def test_resume_of_a_rebound_work_item_is_a_mismatch() -> None:
    state = _state()
    _dispatched_attempt(state)
    state["repair_cycle"]["authority"] = {**BOUND, "key": "f" * 64}

    cmd_repair_cycle(_args(state))

    assert state["repair_cycle"]["attempt_failure"] == "authority-mismatch"


def test_selection_prefers_an_approved_published_repair() -> None:
    state = _state()
    other_key = concern_key(REPOSITORY, "d" * 64)
    assert other_key < KEY  # ranks first on the key tie-break unless approval decides
    other = json.loads(json.dumps(state["work_items"][ITEM]))
    other["id"] = "concerns::other"
    other["detail"]["concern_identity"] = "d" * 64
    for record in ("github_repair_revalidated", "github_repair"):
        other["detail"][record]["key"] = other_key
    state["work_items"]["concerns::other"] = other
    args = _args(state)

    cmd_repair_cycle(args)

    assert args.host.requests[0].authorized_scope.startswith(f"repair {KEY};")
    assert state["repair_cycle"]["authority"] == BOUND


def test_resume_parks_on_unreadable_source_without_failing() -> None:
    state = _state()
    _dispatched_attempt(state)
    unreadable = _Source(AnalysisUnknown("git unavailable"))

    cmd_repair_cycle(_args(state, source=unreadable))

    assert state["repair_cycle"]["parked_reason"] == "source-unreadable"
    assert state["repair_cycle"]["attempt_failure"] is None


def test_config_authority_is_copied_from_the_source_mapping() -> None:
    raw = _config()
    config = CycleConfig.from_mapping(raw)
    raw["authority"]["approvals"].append(_approval(key="other"))  # type: ignore[index]

    assert config.authority == _config()["authority"]
