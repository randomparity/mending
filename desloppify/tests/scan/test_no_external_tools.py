"""``scan --no-external-tools`` starts no program and drops phases that tried (#75)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from desloppify.base.process_guard import deny_process_creation
from desloppify.cli import create_parser
from desloppify.engine.planning.scan import _run_phases
from desloppify.languages.framework import DetectorPhase
from desloppify.state import MergeScanOptions, empty_state, make_issue, merge_scan

_FIXTURES = {
    "rust": {
        "Cargo.toml": '[package]\nname = "m"\nversion = "0.1.0"\nedition = "2021"\n',
        "build.rs": (
            "fn main() {\n"
            '    std::fs::write(concat!(env!("CARGO_MANIFEST_DIR"), "/MARKER"), "x").unwrap();\n'
            "}\n"
        ),
        "src/lib.rs": "pub fn f(x: i32) -> i32 {\n    x + 1\n}\n",
    },
    "javascript": {
        "package.json": '{"name": "j", "version": "1.0.0"}\n',
        "eslint.config.js": (
            'require("fs").writeFileSync(__dirname + "/MARKER", "x");\nmodule.exports = [];\n'
        ),
        "src/a.js": "export function f(x) {\n  return x + 1;\n}\n",
        "node_modules/.bin/eslint": '#!/bin/sh\ntouch "$PWD/MARKER"\n',
        "node_modules/.bin/jscpd": '#!/bin/sh\ntouch "$PWD/MARKER"\n',
    },
    "python": {
        "pyproject.toml": '[project]\nname = "p"\n',
        "src/p/__init__.py": "def f(x):\n    return x + 1\n",
    },
}
_SHIMMED = ("cargo", "npx", "node", "ruff", "bandit")


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _checkout(tmp_path: Path, language: str) -> Path:
    root = tmp_path / language
    for name, text in _FIXTURES[language].items():
        _write_executable(root / name, text)
    return root


def _scan(root: Path, tmp_path: Path, *, flagged: bool, path: str | None = None) -> int:
    """Scan as the refresh does; a shim ``path`` also gets an empty HOME so ``sh -l``
    profiles cannot put real tools ahead of the shims."""
    flag = ["--no-external-tools"] if flagged else []
    env = {**os.environ, "DESLOPPIFY_DENY_PLUGINS": "1"}
    if path is not None:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env.update(PATH=path, HOME=str(home))
    argv = [sys.executable, "-P", "-m", "desloppify", "scan", "--no-badge", *flag,
            "--state", str(tmp_path / f"{root.name}-state.json")]
    return subprocess.run(  # nosec B603
        argv, cwd=root, env=env, timeout=300, check=False, capture_output=True
    ).returncode


def _shim_path(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.log"
    for name in _SHIMMED:
        _write_executable(tmp_path / "shims" / name, f'#!/bin/sh\necho "$0 $*" >> "{log}"\nexit 1\n')
    return f"{tmp_path / 'shims'}{os.pathsep}{os.environ['PATH']}", log


@pytest.mark.parametrize("language", sorted(_FIXTURES))
def test_flagged_scan_starts_no_tool(tmp_path, language) -> None:
    root = _checkout(tmp_path, language)
    path, log = _shim_path(tmp_path)

    assert _scan(root, tmp_path, flagged=True, path=path) == 0
    assert not log.exists()
    assert not (root / "MARKER").exists()


@pytest.mark.parametrize("language", sorted(_FIXTURES))
def test_unflagged_scan_calls_the_tools(tmp_path, language) -> None:
    root = _checkout(tmp_path, language)
    path, log = _shim_path(tmp_path)

    _scan(root, tmp_path, flagged=False, path=path)
    assert log.exists()


@pytest.mark.parametrize(("language", "tool"), [("rust", "cargo"), ("javascript", "npx")])
def test_flagged_scan_runs_no_real_tool(tmp_path, language, tool) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} is not installed")
    root = _checkout(tmp_path, language)

    assert _scan(root, tmp_path, flagged=True) == 0
    assert not (root / "MARKER").exists()


def _spawning_phase() -> DetectorPhase:
    def run(_path, _lang):
        subprocess.run([sys.executable, "-c", "pass"], check=False)  # nosec B603
        return [], {"spawned": 1}

    return DetectorPhase("spawn", run)


def _local_phase() -> DetectorPhase:
    return DetectorPhase("local", lambda _path, _lang: ([{"id": "local"}], {"local": 1}))


def _lang() -> SimpleNamespace:
    return SimpleNamespace(review_cache={}, detector_coverage={})


def test_runner_drops_a_phase_that_tried(tmp_path, capsys) -> None:
    with deny_process_creation():
        issues, potentials = _run_phases(
            tmp_path, _lang(), [_spawning_phase(), _local_phase()]
        )

    assert issues == [{"id": "local"}]
    assert potentials == {"local": 1}
    assert "spawn... skipped: needs an external tool" in capsys.readouterr().err


def test_runner_reraises_without_a_refusal(tmp_path) -> None:
    def broken(_path, _lang):
        raise ValueError("phase bug")

    with deny_process_creation(), pytest.raises(ValueError, match="phase bug"):
        _run_phases(tmp_path, _lang(), [DetectorPhase("broken", broken)])


def test_scan_parser_accepts_no_external_tools() -> None:
    parser = create_parser()
    assert parser.parse_args(["scan"]).no_external_tools is False
    assert parser.parse_args(["scan", "--no-external-tools"]).no_external_tools is True


def test_dropped_phase_keeps_findings_across_runs(tmp_path) -> None:
    def cached_tool(_path, lang):
        cache = lang.review_cache.setdefault("detectors", {})
        if "fake" in cache:
            return [], {"fake": 1}
        cache["fake"] = {"entries": []}
        lang.detector_coverage["fake"] = {"status": "reduced"}
        subprocess.run([sys.executable, "-c", "pass"], check=False)  # nosec B603
        return [], {"fake": 1}

    lang = _lang()
    phase = DetectorPhase("fake", cached_tool)
    with deny_process_creation():
        first = _run_phases(tmp_path, lang, [phase])
        second = _run_phases(tmp_path, lang, [phase])

    assert first == second == ([], {})
    assert lang.review_cache == {} and lang.detector_coverage == {}
    state = empty_state()
    issue = make_issue("fake", "a.py", "x", tier=3, confidence="medium", summary="s")
    issue["lang"] = "python"
    state["issues"][issue["id"]] = issue
    merge_scan(state, [], MergeScanOptions(lang="python", potentials=second[1]))
    assert state["issues"][issue["id"]]["status"] == "open"
