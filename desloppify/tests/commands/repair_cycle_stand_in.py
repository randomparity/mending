"""Scripted Claude Code host and fake ``gh`` for the repair-cycle matrix (#32).

Run as ``python repair_cycle_stand_in.py claude|gh ARGS...``; stdlib only, so
it starts fast and never imports the code under test. Everything it reads and
records lives in the directory named by ``STAND_IN_WORLD``:

- ``world.json``: the fake GitHub (repository, issues, pull requests, the
  account ``gh`` is signed in as) and the log of every ``gh`` argv, read and
  written under an exclusive ``flock``;
- ``host.json``: the scripted steps of a ``claude -p`` run;
- ``launches.jsonl`` and ``pids.jsonl``: each launch and each spawned process.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

WORLD = Path(os.environ.get("STAND_IN_WORLD", "."))
ATTEMPT_TAG = "Mending-Attempt"
WAIT_SECONDS = 30.0
LOGIN = "mending-host"
_HELP = (
    "--output-format <format>  text, json, or stream-json\n"
    "--forward-subagent-text\n"
    "--max-budget-usd <amount>  (only works with --print)\n"
)


def _append(name: str, entry: dict) -> None:
    with (WORLD / name).open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


@contextmanager
def _world() -> Iterator[dict]:
    with (WORLD / "world.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = WORLD / "world.json"
        world = json.loads(path.read_text())
        yield world
        path.write_text(json.dumps(world))


def _options(argv: list[str]) -> dict[str, str]:
    pairs = zip(argv, argv[1:], strict=False)
    return {flag: value for flag, value in pairs if flag.startswith("--")}


def _pick(item: dict, fields: str) -> dict:
    return {field: item.get(field) for field in fields.split(",")}


def _gh(argv: list[str]) -> int:
    with _world() as world:
        world["log"].append(argv)
        kind, action, options = argv[0], argv[1], _options(argv)
        items = world["issues" if kind == "issue" else "prs"]
        if (kind, action) == ("repo", "view"):
            output: object = {"nameWithOwner": argv[2]}
        elif action == "list":
            term = options.get("--search", "")
            output = [_pick(item, options["--json"]) for item in items if term in item["body"]]
        elif (kind, action) == ("issue", "view"):
            found = next(item for item in items if item["number"] == int(argv[2]))
            output = _pick(found, options["--json"])
        elif kind == "api":
            output = _api(world, argv[-1])
        elif action == "create":
            number = 1 + max((i["number"] for i in world["issues"] + world["prs"]), default=0)
            path = "issues" if kind == "issue" else "pull"
            url = f"https://github.com/{options['--repo']}/{path}/{number}"
            files = json.loads(os.environ.get("STAND_IN_PR_FILES", "[]"))
            head_repo = os.environ.get("STAND_IN_HEAD_REPO") or options["--repo"]
            items.append({"number": number, "url": url, "state": "OPEN",
                          "title": options["--title"], "body": options["--body"], "files": files,
                          "author": {"login": world.get("login", LOGIN)},
                          "head": {"ref": options.get("--head"), "repo": head_repo}})
            output = url
        else:
            print(f"stand-in gh: unsupported {argv}", file=sys.stderr)
            return 2
    print(output if isinstance(output, str) else json.dumps(output))
    return 0


def _api(world: dict, endpoint: str) -> object:
    """Answer the REST reads: the signed-in user, pull requests by head, or one pull request."""
    path, _, query = endpoint.partition("?")
    if path == "user":
        return {"login": world.get("login", LOGIN)}
    if path == f"repos/{world['repository']}/pulls":
        owner, _, ref = dict(pair.split("=", 1) for pair in query.split("&"))["head"].partition(":")
        return [[
            {"html_url": item["url"], "head": {"ref": item["head"]["ref"],
                                               "repo": {"full_name": item["head"]["repo"]}}}
            for item in world["prs"]
            if item["head"]["ref"] == ref and item["head"]["repo"].split("/")[0] == owner
        ]]
    match = re.fullmatch(rf"repos/{re.escape(world['repository'])}/pulls/(\d+)(/files)?", path)
    if match is None:
        raise SystemExit(f"stand-in gh: unsupported api endpoint {endpoint}")
    found = next(item for item in world["prs"] if item["number"] == int(match[1]))
    if match[2] is None:
        state = "open" if found["state"] == "OPEN" else "closed"
        return {"state": state, "changed_files": len(found["files"])}
    files = [{"filename": name, "status": "modified"} for name in found["files"]]
    return [files[start:start + 100] for start in range(0, len(files), 100)] or [[]]


def _wait_for_go() -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not (WORLD / "go").exists() and time.monotonic() < deadline:
        time.sleep(0.02)


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def _spawn(code: str, *, new_session: bool = False) -> None:
    child = subprocess.Popen([sys.executable, "-c", code], start_new_session=new_session)
    _append("pids.jsonl", {"pid": child.pid})


def _create(kind: str, attempt: str, branch: str, step: dict) -> None:
    repository = json.loads((WORLD / "world.json").read_text())["repository"]
    tag = f"\n{ATTEMPT_TAG}: {step.get('attempt', attempt)}\n" if step.get("tag", True) else ""
    env = {**os.environ, "STAND_IN_PR_FILES": json.dumps(step.get("files", [])),
           "STAND_IN_HEAD_REPO": step.get("head_repo", "")}
    head = ["--head", step.get("branch", branch)] if kind == "pr" else []
    subprocess.run(
        ["gh", kind, "create", "--repo", repository, *head, "--title", f"Repair {kind}",
         "--body", f"Scripted {kind}.\n{tag}"],
        env=env, check=True, stdout=subprocess.DEVNULL,
    )


def _step(step: dict, attempt: str, branch: str) -> int | None:
    action = step["do"]
    if action == "assistant":
        parent = "toolu_1" if step.get("nested") else None
        for _ in range(step["count"]):
            _emit({"type": "assistant", "parent_tool_use_id": parent,
                   "message": {"id": uuid.uuid4().hex, "content": []}})
    elif action == "nested":
        # A model process started through a host tool: its messages never reach the stream.
        event = json.dumps({"type": "assistant", "message": {"id": "nested"}})
        _spawn(f"import time\nwith open({str(WORLD / 'nested.jsonl')!r}, 'a') as fh:\n"
               f"    fh.write({event!r} * {step['count']})\ntime.sleep({WAIT_SECONDS})\n")
    elif action == "child":
        ignore = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        sleep = f"import time; time.sleep({WAIT_SECONDS})\n"
        _spawn((ignore if step.get("ignore_term") else "") + sleep,
               new_session=bool(step.get("setsid")))
    elif action in ("pr", "issue"):
        _create(action, attempt, branch, step)
    elif action == "wait":
        _wait_for_go()
    elif action == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(WAIT_SECONDS)
    elif action == "result":
        _emit({"type": "result", "subtype": step.get("subtype", "success"),
               "is_error": step.get("is_error", False), "total_cost_usd": step.get("cost", 0.5)})
    elif action == "garbage":
        print("not json", flush=True)
    elif action == "exit":
        return int(step["code"])
    return None


def _claude(argv: list[str]) -> int:
    if argv == ["--version"]:
        (WORLD / "probed").touch()
        if (WORLD / "hold-probe").exists():  # hold Mending between dispatch intent and launch
            (WORLD / "probing").touch()
            _wait_for_go()
        print("2.1.289 (Claude Code)")
        return 0
    if argv == ["--help"]:
        print(_HELP, end="")
        return 0
    prompt = sys.stdin.read()
    found = re.search(r"^Attempt ID: (\S+)$", prompt, re.MULTILINE)
    attempt = found.group(1) if found else ""
    found = re.search(r"^Branch: (\S+)$", prompt, re.MULTILINE)
    branch = found.group(1) if found else ""
    _append("launches.jsonl", {
        "pid": os.getpid(), "argv": argv, "attempt": attempt,
        "marker": os.environ.get("MENDING_HOST_SESSION"),
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
        "background": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
    })
    for step in json.loads((WORLD / "host.json").read_text()):
        code = _step(step, attempt, branch)
        if code is not None:
            return code
    return 0


if __name__ == "__main__":
    role, args = sys.argv[1], sys.argv[2:]
    sys.exit(_claude(args) if role == "claude" else _gh(args))
