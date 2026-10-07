# Plan: refresh scan starts no external tools (#75)

Goal: `desloppify scan --no-external-tools` starts no program, drops every
phase that tried, and the repair-cycle refresh uses it.

Architecture: a process-creation audit hook in `desloppify/base/process_guard.py`
(spec: `docs/workflow/specs/2026-10-07-refresh-no-external-tools-design.md`,
ADR 0017). `cmd_scan` opens the guard when the flag is set; the phase runner in
`desloppify/engine/planning/scan.py` drops a phase whose run raised the refusal
count and skips the prefetch while the guard is open.

Tech stack: Python ≥ 3.11 (`requires-python`), pytest, uv-managed venv.

Expected implementation size: 220–290 changed lines (M) — guard module ~45,
runner ~35, flag/cmd ~15, refresh/doc ~10, tests ~150.

## Global Constraints

- Python `>=3.11`; no new dependency.
- Interactive `scan` without the flag behaves exactly as before.
- In `desloppify/app/commands/repair_cycle.py` change only `_refresh`'s argv.
- Guardrails (each CI-gated): `make lint`, `make typecheck`, `make arch`,
  `make ci-contracts`, `make tests`, `make tests-full`, `make package-smoke`;
  records: `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
- Conventional commits, ≤72-char subject.

## File map

| File | Change | Owns |
|---|---|---|
| `desloppify/base/process_guard.py` | create | the hook, guard context, refusal count |
| `desloppify/engine/planning/scan.py` | modify `_run_phases`, `_generate_issues_from_lang` | dropping phases that tried |
| `desloppify/app/cli_support/parser_groups.py` | modify `_add_scan_parser` | the flag |
| `desloppify/app/commands/scan/cmd.py` | modify `cmd_scan` | opening the guard |
| `desloppify/app/commands/repair_cycle.py` | modify `_refresh` argv | refresh uses the flag |
| `docs/systemd/repair-cycle.md` | modify refresh paragraph | operator disclosure |
| `desloppify/tests/base/test_process_guard.py` | create | guard tests |
| `desloppify/tests/scan/test_no_external_tools.py` | create | runner + marker tests |
| `desloppify/tests/commands/test_repair_cycle.py` | modify argv test | refresh argv |

## Task 1 — process guard

Interfaces (later tasks rely on): `deny_process_creation() -> ContextManager[None]`,
`process_creation_denied() -> bool`, `refusal_count() -> int`,
`class ExternalToolRefused(PermissionError)`.

Verification:
- Contract: inside the guard `subprocess.run`, `os.system`, and `os.spawnv`
  raise `ExternalToolRefused` and the count rises; outside, `subprocess.run`
  works.
  Mode: focused-test — `desloppify/tests/base/test_process_guard.py`; red:
  `ModuleNotFoundError: desloppify.base.process_guard`; green:
  `uv run --locked pytest -q desloppify/tests/base/test_process_guard.py`.

Steps:
1. Write the test:

```python
import os
import subprocess
import sys

import pytest

from desloppify.base.process_guard import (
    ExternalToolRefused, deny_process_creation, process_creation_denied, refusal_count,
)


def test_guard_refuses_process_creation_only_while_open(tmp_path) -> None:
    marker = tmp_path / "ran"
    start = refusal_count()
    with deny_process_creation():
        assert process_creation_denied()
        with pytest.raises(ExternalToolRefused):
            subprocess.run([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], check=False)
        with pytest.raises(ExternalToolRefused):
            os.system(f"touch {marker}")
        with pytest.raises(ExternalToolRefused):
            os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", "pass"])
    assert refusal_count() >= start + 3
    assert not marker.exists()
    assert not process_creation_denied()
    assert subprocess.run([sys.executable, "-c", "pass"], check=False).returncode == 0
```

2. Run it; expect the `ModuleNotFoundError` red.
3. Create the module:

```python
"""Refuse process creation while a guard is open (#75, ADR 0017).

CPython raises these audit events before it starts a program, so a refused call
runs nothing. An installed hook cannot be removed; it acts only while open.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

# os.fork: os.spawn* forks first and execs in the child, out of the parent's count.
_REFUSED_EVENTS = frozenset({
    "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.startfile",
    "os.system", "pty.spawn", "subprocess.Popen",
})
_installed = False
_depth = 0
_refusals = 0


class ExternalToolRefused(PermissionError):
    """Raised instead of starting a program while process creation is denied."""


def _audit(event: str, _args: tuple[object, ...]) -> None:
    global _refusals
    if _depth and event in _REFUSED_EVENTS:
        _refusals += 1
        raise ExternalToolRefused(f"external tools are disabled for this scan ({event})")


@contextmanager
def deny_process_creation() -> Iterator[None]:
    """Refuse every audited process creation in this process until exit."""
    global _installed, _depth
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1


def process_creation_denied() -> bool:
    return _depth > 0


def refusal_count() -> int:
    return _refusals
```

4. Run the green command; expect `1 passed`. Commit
   `feat(scan): add a guard that refuses process creation`.

## Task 2 — drop phases that tried, and the scan flag

Interfaces: consumes Task 1's four names. Provides `scan --no-external-tools`
(`args.no_external_tools: bool`) and `_run_phases` dropping refused phases.

Verification:
- Contract: under the guard, a phase that starts a program contributes no issues
  or potentials and stderr names it `skipped: needs an external tool`; an
  in-process phase still contributes; without the guard an exception from a
  phase propagates. Mode: focused-test — `test_runner_drops_a_phase_that_tried`
  and `test_runner_reraises_without_a_refusal` in
  `desloppify/tests/scan/test_no_external_tools.py`; red: the spawning phase's
  `ExternalToolRefused` escapes `_run_phases`; green:
  `uv run --locked pytest -q desloppify/tests/scan/test_no_external_tools.py -k runner`.
- Contract: the parser accepts `--no-external-tools`, default `False`.
  Mode: focused-test — `test_scan_parser_accepts_no_external_tools`; red:
  argparse exit 2; green: same file `-k parser`.
- Contract: a real scan subprocess with the flag starts no tool. Mode:
  focused-test — `test_flagged_scan_starts_no_tool` (shim arm, parametrized
  rust/javascript) and `test_flagged_scan_runs_no_real_tool` (skipped without
  `cargo`/`npx`); red: argparse exits 2 (unrecognized `--no-external-tools`)
  and the returncode-0 assertion fails; green: same file.
- Contract: a dropped phase's writes to `lang.review_cache` and
  `lang.detector_coverage` are undone, so a dropped detector's open finding
  stays open after two flagged runs over unchanged input. Mode: focused-test —
  `test_dropped_phase_keeps_findings_across_runs`: a fake `lang`
  (`SimpleNamespace(review_cache={}, detector_coverage={})`) and a phase that
  returns its cached result when `review_cache["detectors"]["fake"]` holds
  one, else writes an empty cache entry and calls `subprocess.run`; run
  `_run_phases` twice under the guard; assert both runs drop it (no `"fake"`
  potentials), `review_cache == {}` afterwards, and that `merge_scan` on a state
  holding an open `"fake"` finding, with the second run's potentials, leaves it
  `open`. Red: the second run keeps the phase (cache hit, no refusal);
  green: `uv run --locked pytest -q desloppify/tests/scan/test_no_external_tools.py -k across_runs`.
- Contract: without the flag the same fixture calls the shim (bite).
  Mode: focused-test — `test_unflagged_scan_calls_the_tools`.

Steps:
1. Write the tests. Fixtures: Rust — `Cargo.toml`
   (`[package] name="m" version="0.1.0" edition="2021"`), `build.rs` writing
   `MARKER` into `CARGO_MANIFEST_DIR`, `src/lib.rs`. JavaScript —
   `package.json`, `eslint.config.js` writing `MARKER` via `fs.writeFileSync`,
   `src/a.js`, and executable `node_modules/.bin/eslint` and
   `node_modules/.bin/jscpd` shell scripts that `touch MARKER`. Shim dir holds
   `cargo`, `npx`, `node` scripts appending `$0 $*` to a log. Each scan runs
   `[sys.executable, "-P", "-m", "desloppify", "scan", "--no-badge", *flag,
   "--state", str(tmp_path / "state.json")]` with `cwd=` the fixture,
   `HOME=` a tmp dir (keeps `sh -l` profiles out), `DESLOPPIFY_DENY_PLUGINS=1`,
   `timeout=300`. Assert returncode 0, no `MARKER`, empty log (flagged); log
   non-empty (unflagged). Runner tests build
   `DetectorPhase("spawn", run)` / `DetectorPhase("local", run)` and call
   `_run_phases(tmp_path, lang, phases)` with `lang = SimpleNamespace(review_cache={}, detector_coverage={})` inside `deny_process_creation()`.
2. Run them; expect the reds above.
3. Add to `_add_scan_parser` after `--skip-slow`:

```python
    p_scan.add_argument(
        "--no-external-tools",
        action="store_true",
        help="Start no external program (linters, compilers, npx); skip phases that need one",
    )
```

4. In `cmd_scan`, rename the current body to `_cmd_scan(args)` and add:

```python
def cmd_scan(args: argparse.Namespace) -> None:
    """Run all detectors, update persistent state, show diff."""
    guard = deny_process_creation() if getattr(args, "no_external_tools", False) else nullcontext()
    with guard:
        _cmd_scan(args)
```

5. In `engine/planning/scan.py`, replace the loop body of `_run_phases` with a
   call to a new `_run_phase` and skip `None`:

```python
def _run_phase(
    path: Path, lang: LangRun, phase: DetectorPhase
) -> tuple[list[Issue], dict[str, int]] | None:
    """The phase's results, or None when it tried to start a program under the guard.

    A dropped phase's writes to the caches the scan persists are undone, so a later
    run cannot read a refusal-degraded result as a clean one (#75).
    """
    saved = (
        copy.deepcopy((lang.review_cache, lang.detector_coverage))
        if process_creation_denied()
        else None
    )
    before = refusal_count()
    try:
        result = phase.run(path, lang)
    except Exception:
        if refusal_count() == before:
            raise
        result = None
    if refusal_count() == before:
        return result
    if saved is not None:
        for live, kept in zip((lang.review_cache, lang.detector_coverage), saved):
            live.clear()
            live.update(kept)
    return None
```

   (add `import copy`; `refusal_count`, `process_creation_denied` from
   `desloppify.base.process_guard`), printing
   `_stderr(f"  [{idx}/{total}] {phase.label}... skipped: needs an external tool")`
   on `None`. In `_generate_issues_from_lang`, call
   `prewarm_review_phase_detectors` only when `not process_creation_denied()`.
6. Run the green commands; then `make lint typecheck arch`. Commit
   `feat(scan): add --no-external-tools and drop phases that need a tool`.

## Task 3 — refresh uses the flag; guide

Verification:
- Contract: `_refresh` argv is `[..., "scan", "--no-badge", "--no-external-tools", "--state", <path>]`.
  Mode: focused-test — update the expected argv in
  `test_production_refresh_scans_into_the_cycle_state_file`; red: argv
  mismatch; green: `uv run --locked pytest -q desloppify/tests/commands/test_repair_cycle.py -k production_refresh`.
- Contract: guide paragraph. Mode: task-test-not-applicable — operator prose
  with no executable consumer.

Steps:
1. Update the test's expected argv; run; expect the mismatch red.
2. In `_refresh` add `"--no-external-tools",` after `"--no-badge",` and extend
   the comment above `env`: "and the scan starts no external program (#75)".
3. In `docs/systemd/repair-cycle.md` replace "The scan's external language tools
   (for example `cargo check` or `npx eslint`) still run in the checkout and can
   execute code it contains (#75)." with: "The refresh scan also starts no
   external program (`--no-external-tools`): phases that need one (for example
   `cargo check`, `npx eslint`, `ruff`, `bandit`, `jscpd`) are skipped and named in
   the journal, so the cycle state does not track their findings. Interactive
   scans still run them."
4. Green command, then the full guardrail list. Commit
   `fix(repair-cycle): refresh with a scan that starts no external tools`.
