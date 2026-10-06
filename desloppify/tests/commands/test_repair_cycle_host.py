from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from desloppify.app.commands import repair_cycle_host
from desloppify.app.commands.repair_cycle_host import (
    ATTEMPT_TAG,
    MARKER_VARIABLE,
    ClaudeHostAdapter,
    HostLookupError,
    HostReferences,
    HostRequest,
    PullRequest,
    host_session_id,
)
from desloppify.engine.repair_cycle import BudgetAdmission, CycleConfig

_STAND_IN = """#!{python}
import json, os, signal, subprocess, sys, time
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_HOST_VERSION", "2.1.289 (Claude Code)"))
    sys.exit(0)
if sys.argv[1:] == ["--help"]:
    help_mode = os.environ.get("FAKE_HOST_HELP", "full")
    if help_mode == "fail":
        sys.exit(1)
    if help_mode == "slow":
        time.sleep(5)
    print("--output-format <format>  text, json, or stream-json")
    print("--forward-subagent-text")
    if help_mode != "bare":
        print("--max-budget-usd <amount>")
    sys.exit(0)
mode = os.environ["FAKE_HOST_MODE"]
record = os.environ["FAKE_HOST_RECORD"]
seen = {{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
         "marker": os.environ.get("{marker}"),
         "background": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
         "pids": [os.getpid()]}}
def emit(event):
    print(json.dumps(event), flush=True)
def assistant(message_id, parent=None):
    emit({{"type": "assistant", "parent_tool_use_id": parent,
          "message": {{"id": message_id, "content": []}}}})
def usage():
    assistant("m1")
    assistant("m1")
    assistant("m2", parent="toolu_1")
def result(is_error, cost, subtype="success", **extra):
    emit({{"type": "result", "subtype": subtype, "is_error": is_error,
          "total_cost_usd": cost, **extra}})
def child(new_session, ignore_term):
    code = "import os, signal, time\\n"
    if new_session:
        code += "os.setsid()\\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
    code += "time.sleep(60)\\n"
    return subprocess.Popen([sys.executable, "-c", code]).pid
if mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    seen["pids"] += [child(False, True), child(True, True)]
elif mode == "orphan":
    seen["pids"].append(child(True, False))
with open(record + ".tmp", "w") as fh:
    json.dump(seen, fh)
os.rename(record + ".tmp", record)
if mode in ("ok", "orphan"):
    usage()
    result(False, 0.42, result="draft PR")
elif mode == "fail":
    usage()
    result(True, 0.1, subtype="error_max_budget_usd")
    print("host said no", file=sys.stderr)
    sys.exit(3)
elif mode == "crash":
    usage()
    result(True, 0, subtype="error_during_execution")
    sys.exit(1)
elif mode == "garbage":
    print("not json")
elif mode == "hang":
    time.sleep(60)
elif mode == "chatty":
    for n in range(1200):
        assistant(f"c{{n}}")
        time.sleep(0.05)
"""
_ADMISSION = BudgetAdmission(Decimal("1.5"), 100)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _kill_recorded(record: Path) -> None:
    if not record.exists():
        return
    for pid in json.loads(record.read_text())["pids"]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    exe = tmp_path / "claude"
    exe.write_text(_STAND_IN.format(python=sys.executable, marker=MARKER_VARIABLE))
    exe.chmod(0o755)
    manifest = tmp_path / "adept" / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "adept", "version": "7.2.0"}))
    skill = tmp_path / "adept" / "skills" / "quest" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# quest\n")
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_HOST_RECORD", str(record))
    yield {"exe": str(exe), "skills": str(manifest.parent.parent), "record": record}
    _kill_recorded(record)


def _config(host: dict, **overrides: object) -> CycleConfig:
    raw: dict[str, object] = {
        "enabled": True,
        "repository": "owner/repository",
        "model": "sonnet",
        "cost_cap_usd": 1,
        "host_executable": host["exe"],
        "adept_skills_dir": host["skills"],
        "adept_skills_version": "7.2.0",
        **overrides,
    }
    return CycleConfig.from_mapping({key: value for key, value in raw.items() if value is not None})


def _request(tmp_path: Path, seconds: float = 30) -> HostRequest:
    return HostRequest(
        attempt_id=uuid.uuid4().hex,
        brief="Fix the flaky parser.",
        source_revision="0" * 40,
        authorized_scope="desloppify/parser.py",
        repo_root=tmp_path,
        deadline=datetime.now(UTC) + timedelta(seconds=seconds),
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"model": None}, "missing-model"),
        ({"host_executable": "/nonexistent/claude"}, "missing-host"),
        ({"adept_skills_dir": None}, "missing-host-skills"),
        ({"adept_skills_version": None}, "missing-host-skills"),
        ({"adept_skills_dir": "/nonexistent"}, "missing-host-skills"),
        ({"adept_skills_version": "7.1.0"}, "host-skills-mismatch"),
    ],
)
def test_preflight_parks_without_launch(host, tmp_path, monkeypatch, overrides, reason):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    outcome = ClaudeHostAdapter(_config(host, **overrides)).run(_request(tmp_path), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("parked", reason)
    assert not host["record"].exists()


def test_wrong_manifest_name_and_past_deadline_park(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    adapter = ClaudeHostAdapter(_config(host))
    assert adapter.run(_request(tmp_path, seconds=-1), _ADMISSION).reason == "runtime-exhausted"
    Path(host["skills"], ".claude-plugin", "plugin.json").write_text(
        json.dumps({"name": "other", "version": "7.2.0"})
    )
    assert adapter.run(_request(tmp_path), _ADMISSION).reason == "host-skills-mismatch"
    assert not host["record"].exists()


def test_completed_run_reports_identity(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    request = _request(tmp_path)
    outcome = ClaudeHostAdapter(_config(host)).run(request, _ADMISSION)
    session = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mending-attempt:{request.attempt_id}"))
    assert host_session_id(request.attempt_id) == session
    assert (outcome.state, outcome.session_id, outcome.skills_version) == (
        "completed",
        session,
        "7.2.0",
    )
    assert outcome.result is not None and outcome.result["result"] == "draft PR"
    seen = json.loads(host["record"].read_text())
    argv = seen["argv"]
    assert argv[:5] == [
        "-p", "--output-format", "stream-json", "--verbose", "--forward-subagent-text"
    ]
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"
    assert seen["background"] == "1"
    assert argv[argv.index("--session-id") + 1] == session
    assert argv[argv.index("--plugin-dir") + 1] == host["skills"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert not any("dangerously" in arg for arg in argv)
    assert seen["marker"] == session
    assert "Fix the flaky parser." in seen["stdin"]
    assert f"Attempt ID: {request.attempt_id}" in seen["stdin"]
    assert f"'{ATTEMPT_TAG}: {request.attempt_id}'" in seen["stdin"]


@pytest.mark.parametrize(("cost", "flag"), [("1E-7", "0.0000001"), ("1E+1", "10")])
def test_budget_flag_is_fixed_point(host, tmp_path, monkeypatch, cost, flag):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    admission = BudgetAdmission(Decimal(cost), 100)
    ClaudeHostAdapter(_config(host)).run(_request(tmp_path), admission)
    argv = json.loads(host["record"].read_text())["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == flag


@pytest.mark.parametrize(
    ("mode", "state", "reason", "cost"),
    [
        ("ok", "completed", None, Decimal("0.42")),
        ("fail", "failed", "host-error", Decimal("0.1")),
        ("crash", "failed", "host-error", None),
    ],
)
def test_stream_usage_is_measured(host, tmp_path, monkeypatch, mode, state, reason, cost):
    monkeypatch.setenv("FAKE_HOST_MODE", mode)
    outcome = ClaudeHostAdapter(_config(host)).run(_request(tmp_path), _ADMISSION)
    assert (outcome.state, outcome.reason) == (state, reason)
    assert (outcome.cost_usd, outcome.calls) == (cost, 2)


def test_call_limit_stops_whole_tree(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "chatty")
    adapter = ClaudeHostAdapter(_config(host), grace_seconds=0.5)
    outcome = adapter.run(_request(tmp_path), BudgetAdmission(Decimal("1"), 3))
    assert (outcome.state, outcome.reason) == ("stopped", "call-limit")
    assert outcome.calls > 3 and outcome.cost_usd is None
    assert all(_gone(pid) for pid in json.loads(host["record"].read_text())["pids"])


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("FAKE_HOST_HELP", "bare"),
        ("FAKE_HOST_HELP", "fail"),
        ("FAKE_HOST_HELP", "slow"),
        ("FAKE_HOST_VERSION", "2.1.274 (Claude Code)"),
        ("FAKE_HOST_VERSION", "unknown"),
        (None, None),
    ],
)
def test_unenforceable_limit_parks_without_launch(host, tmp_path, monkeypatch, variable, value):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    monkeypatch.setattr(repair_cycle_host, "_PROBE_SECONDS", 0.5)
    if variable is None:
        monkeypatch.setattr(repair_cycle_host, "_PROC_ROOT", tmp_path / "no-proc")
    else:
        monkeypatch.setenv(variable, value)
    outcome = ClaudeHostAdapter(_config(host)).run(_request(tmp_path), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("parked", "unenforceable-limit")
    assert not host["record"].exists()


@pytest.mark.parametrize(
    ("mode", "reason", "detail"),
    [("fail", "host-error", "host said no"), ("garbage", "invalid-host-output", None)],
)
def test_nonzero_and_invalid_output_fail(host, tmp_path, monkeypatch, mode, reason, detail):
    monkeypatch.setenv("FAKE_HOST_MODE", mode)
    outcome = ClaudeHostAdapter(_config(host)).run(_request(tmp_path), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("failed", reason)
    assert (outcome.detail or "").strip() == (detail or "")


def test_timeout_stops_whole_tree(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "hang")
    adapter = ClaudeHostAdapter(_config(host), grace_seconds=0.5)
    outcome = adapter.run(_request(tmp_path, seconds=1.5), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("stopped", "timeout")
    pids = json.loads(host["record"].read_text())["pids"]
    assert len(pids) == 3
    assert all(_gone(pid) for pid in pids)


def test_exit_with_live_child_stops_child(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "orphan")
    outcome = ClaudeHostAdapter(_config(host), grace_seconds=2).run(_request(tmp_path), _ADMISSION)
    assert outcome.state == "completed"
    assert all(_gone(pid) for pid in json.loads(host["record"].read_text())["pids"])


def test_unverifiable_tree_is_unknown(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "hang")
    monkeypatch.setattr(repair_cycle_host, "_tree_alive", lambda _pgid, _marker: True)
    adapter = ClaudeHostAdapter(_config(host), grace_seconds=0.2)
    outcome = adapter.run(_request(tmp_path, seconds=1), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("unknown", "timeout-survivors")


def test_relative_paths_reach_host_as_absolute(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    config = _config(host, adept_skills_dir="adept", host_executable="./claude")
    outcome = ClaudeHostAdapter(config).run(replace(_request(tmp_path), repo_root=repo), _ADMISSION)
    assert outcome.state == "completed"
    argv = json.loads(host["record"].read_text())["argv"]
    assert argv[argv.index("--plugin-dir") + 1] == host["skills"]


def test_manifest_without_skills_parks(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    (Path(host["skills"]) / "skills" / "quest" / "SKILL.md").unlink()
    outcome = ClaudeHostAdapter(_config(host)).run(_request(tmp_path), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("parked", "missing-host-skills")
    assert not host["record"].exists()


def test_signal_during_launch_is_deferred_until_stoppable(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "hang")
    real_popen = repair_cycle_host.subprocess.Popen

    def popen_then_signal(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        if "-p" not in args[0]:  # capability probes also run through Popen
            return proc
        deadline = time.monotonic() + 10
        while not host["record"].exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        return proc

    monkeypatch.setattr(repair_cycle_host.subprocess, "Popen", popen_then_signal)
    with pytest.raises(SystemExit):
        ClaudeHostAdapter(_config(host), grace_seconds=0.5).run(_request(tmp_path), _ADMISSION)
    assert all(_gone(pid) for pid in json.loads(host["record"].read_text())["pids"])


def test_launch_failure_parks(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "ok")
    request = replace(_request(tmp_path), repo_root=tmp_path / "missing")
    outcome = ClaudeHostAdapter(_config(host)).run(request, _ADMISSION)
    assert (outcome.state, outcome.reason) == ("parked", "host-launch-failed")


def test_missing_proc_is_unknown(host, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_HOST_MODE", "hang")
    monkeypatch.setattr(repair_cycle_host, "_marked_pids", lambda _marker: None)
    adapter = ClaudeHostAdapter(_config(host), grace_seconds=0.2)
    outcome = adapter.run(_request(tmp_path, seconds=1), _ADMISSION)
    assert (outcome.state, outcome.reason) == ("unknown", "timeout-survivors")


@pytest.mark.parametrize(
    ("signum", "expected"),
    [
        (signal.SIGTERM, SystemExit),
        (signal.SIGHUP, SystemExit),
        (signal.SIGINT, KeyboardInterrupt),
    ],
)
def test_cancellation_stops_tree_and_reraises(host, tmp_path, monkeypatch, signum, expected):
    monkeypatch.setenv("FAKE_HOST_MODE", "hang")
    previous = signal.getsignal(signum)

    def cancel_once_launched() -> None:
        deadline = time.monotonic() + 10
        while not host["record"].exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signum)

    timer = threading.Thread(target=cancel_once_launched)
    timer.start()
    with pytest.raises(expected) as raised:
        ClaudeHostAdapter(_config(host), grace_seconds=0.5).run(_request(tmp_path), _ADMISSION)
    timer.join()
    if expected is SystemExit:
        assert raised.value.code == 128 + signum
    assert all(_gone(pid) for pid in json.loads(host["record"].read_text())["pids"])
    assert signal.getsignal(signum) is previous


def test_worker_alive_follows_the_attempt_marker(host, monkeypatch):
    adapter = ClaudeHostAdapter(_config(host))
    attempt = uuid.uuid4().hex
    assert adapter.worker_alive(attempt) is False
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, MARKER_VARIABLE: host_session_id(attempt)},
    )
    try:
        assert adapter.worker_alive(attempt) is True
    finally:
        child.kill()
        child.wait()
    assert adapter.worker_alive(attempt) is False
    monkeypatch.setattr(repair_cycle_host, "_marked_pids", lambda _marker: None)
    assert adapter.worker_alive(attempt) is None


_FAKE_GH = """#!{python}
import os, sys
kind = sys.argv[1]
mode = os.environ.get("FAKE_GH_MODE", "ok")
if mode == "fail":
    print("denied", file=sys.stderr)
    sys.exit(1)
if mode == "garbage":
    print("not json")
    sys.exit(0)
with open(os.environ["FAKE_GH_RECORD"], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\\n")
key = kind.upper()
if kind == "api":
    key += "_FILES" if sys.argv[-1].split("?")[0].endswith("/files") else "_PULL"
print(os.environ["FAKE_GH_" + key])
"""


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def lookup(host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH.format(python=sys.executable))
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_RECORD", str(tmp_path / "gh.log"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    return {"adapter": ClaudeHostAdapter(_config(host)), "repo": repo, "log": tmp_path / "gh.log"}


def test_references_keep_tagged_bodies_and_new_worktrees(lookup, tmp_path, monkeypatch):
    adapter, repo = lookup["adapter"], lookup["repo"]
    attempt = uuid.uuid4().hex
    tag = f"{ATTEMPT_TAG}: {attempt}"
    prior = adapter.worktrees(repo)
    assert prior == (str(repo.resolve()),)
    _git("worktree", "add", "-q", "-b", "feat/x", str(tmp_path / "wt"), cwd=repo)
    monkeypatch.setenv("FAKE_GH_PR", json.dumps([
        {"url": "https://example.invalid/pull/1", "body": f"Fix.\n\n{tag}\n"},
        {"url": "https://example.invalid/pull/2", "body": f"{tag}-other"},
        {"url": "https://example.invalid/pull/3", "body": None},
        "noise",
    ]))
    monkeypatch.setenv("FAKE_GH_ISSUE", json.dumps([
        {"url": "https://example.invalid/issues/4", "body": tag},
        {"url": 5, "body": tag},
    ]))

    found = adapter.references(attempt, "owner/repository", repo, prior)

    assert found == HostReferences(
        ("https://example.invalid/pull/1",),
        ("https://example.invalid/issues/4",),
        (str((tmp_path / "wt").resolve()),),
    )
    calls = lookup["log"].read_text().splitlines()
    assert [call.split()[:2] for call in calls] == [["pr", "list"], ["issue", "list"]]
    assert all("--repo owner/repository --state all --limit 100" in call for call in calls)


@pytest.mark.parametrize(("mode", "message"), [("fail", "gh pr list exited 1: denied"),
                                                 ("garbage", "gh pr list returned invalid JSON")])
def test_references_fail_closed(lookup, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_GH_MODE", mode)
    with pytest.raises(HostLookupError, match=message):
        lookup["adapter"].references("a1", "owner/repository", lookup["repo"], ())


def test_lookup_without_gh_or_git_repository_fails_closed(lookup, tmp_path, monkeypatch):
    with pytest.raises(HostLookupError, match="git worktree list exited"):
        lookup["adapter"].worktrees(tmp_path / "bin")
    monkeypatch.setenv("FAKE_GH_PR", "{}")
    with pytest.raises(HostLookupError):
        lookup["adapter"].references("a1", "owner/repository", lookup["repo"], ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(HostLookupError):
        lookup["adapter"].references("a1", "owner/repository", lookup["repo"], ())


def _files(*names: str, renamed_from: dict[str, str] | None = None) -> list[dict]:
    renamed_from = renamed_from or {}
    return [
        {"filename": name, "status": "renamed", "previous_filename": renamed_from[name]}
        if name in renamed_from else {"filename": name, "status": "modified"}
        for name in names
    ]


def _serve_pull(monkeypatch, state: str, count: int, pages: list[list[dict]]) -> None:
    monkeypatch.setenv("FAKE_GH_API_PULL", json.dumps({"state": state, "changed_files": count}))
    monkeypatch.setenv("FAKE_GH_API_FILES", json.dumps(pages))


def test_pull_requests_read_state_and_every_changed_path(lookup, monkeypatch):
    adapter = lookup["adapter"]
    url = "https://github.com/owner/repository/pull/7"
    first = _files(*(f"src/m{i}.py" for i in range(100)))
    second = _files("src/new.py", renamed_from={"src/new.py": "secret/old.py"})
    _serve_pull(monkeypatch, "closed", 101, [first, second])

    (pr,) = adapter.pull_requests((url,))

    assert pr.url == url and pr.open is False
    assert pr.files == frozenset({entry["filename"] for entry in first} | {
        "src/new.py", "secret/old.py",
    })
    _serve_pull(monkeypatch, "open", 0, [[]])
    renamed = "https://github.com/Owner/Repository/pull/8"
    assert adapter.pull_requests((renamed,)) == (PullRequest(renamed, True, frozenset()),)
    assert lookup["log"].read_text().splitlines() == [
        "api repos/owner/repository/pulls/7",
        "api --paginate --slurp repos/owner/repository/pulls/7/files?per_page=100",
        "api repos/owner/repository/pulls/8",
        "api --paginate --slurp repos/owner/repository/pulls/8/files?per_page=100",
    ]


@pytest.mark.parametrize("count", [101, 99, 3001])
def test_pull_requests_refuse_a_file_list_that_misses_changed_files(lookup, monkeypatch, count):
    _serve_pull(monkeypatch, "open", count, [_files(*(f"f{i}" for i in range(100)))])
    with pytest.raises(HostLookupError, match=f"listed 100 of {count} changed files"):
        lookup["adapter"].pull_requests(("https://github.com/owner/repository/pull/7",))


def test_pull_requests_refuse_a_list_at_the_api_cap(lookup, monkeypatch):
    # GitHub lists at most 3000 files, so a full list cannot show nothing was cut off.
    pages = [[{"filename": f"{page}/{i}"} for i in range(100)] for page in range(30)]
    _serve_pull(monkeypatch, "closed", 3000, pages)
    with pytest.raises(HostLookupError, match="cannot list all 3000 changed files"):
        lookup["adapter"].pull_requests(("https://github.com/owner/repository/pull/7",))


@pytest.mark.parametrize(("pull", "pages"), [
    ({"state": 1, "changed_files": 0}, [[]]),
    ({"state": "open"}, [[]]),
    ({"state": "open", "changed_files": "1"}, [_files("a")]),
    ({"state": "open", "changed_files": True}, [_files("a")]),
    ([], [[]]),
    ({"state": "open", "changed_files": 1}, _files("a")),
    ({"state": "open", "changed_files": 1}, {"filename": "a"}),
    ({"state": "open", "changed_files": 1}, [[{"filename": 3}]]),
    ({"state": "open", "changed_files": 1}, [["a"]]),
    ({"state": "open", "changed_files": 1}, [[{"filename": "a", "previous_filename": 3}]]),
])
def test_pull_requests_reject_malformed_output(lookup, monkeypatch, pull, pages):
    monkeypatch.setenv("FAKE_GH_API_PULL", json.dumps(pull))
    monkeypatch.setenv("FAKE_GH_API_FILES", json.dumps(pages))
    with pytest.raises(HostLookupError, match="gh api"):
        lookup["adapter"].pull_requests(("https://github.com/owner/repository/pull/7",))


def test_pull_requests_fail_closed_when_gh_fails(lookup, monkeypatch):
    for mode, message in (("fail", "gh api exited 1: denied"),
                          ("garbage", "gh api returned invalid JSON")):
        monkeypatch.setenv("FAKE_GH_MODE", mode)
        with pytest.raises(HostLookupError, match=message):
            lookup["adapter"].pull_requests(("https://github.com/owner/repository/pull/7",))


@pytest.mark.parametrize("url", [
    "https://github.com/other/repository/pull/7",
    "https://github.com/owner/repository/pull/7 --web",
    "https://github.com/owner/repository/issues/7",
    "--web",
])
def test_pull_requests_refuse_foreign_urls_without_running_gh(lookup, url):
    with pytest.raises(HostLookupError, match="not a pull request"):
        lookup["adapter"].pull_requests((url,))
    assert not lookup["log"].exists()
