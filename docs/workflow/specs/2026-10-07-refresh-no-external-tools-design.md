# Start no external tools in the repair-cycle refresh scan

Issue: #75 (split out of #63). Builds on ADR 0006 and ADR 0016.
Decision record: [ADR 0017](../../adr/0017-refresh-starts-no-external-tools.md).

## Problem

`_refresh` (`desloppify/app/commands/repair_cycle.py`) runs
`python -P -m desloppify scan --no-badge --state <cycle state>` in the target
checkout. The scan's phases start external programs there, and several run code
the checkout controls. Observed at `e36caa07` with an audit hook logging process
creation on tiny fixtures:

- Rust: `cargo clippy`, `cargo check`, `cargo rustdoc` (via `/bin/sh -lc`) and
  `cargo metadata`; a `build.rs` that writes a marker file ran.
- JavaScript: `npx eslint .`; an `eslint.config.js` that writes a marker ran.
- Every language: the boilerplate-duplication prefetch runs `npx --yes jscpd`,
  which resolves a binary from the checkout's `node_modules`.
- Python: `python -m bandit` and `python -m ruff` with the checkout as working
  directory, so a checkout `bandit/` or `ruff/` package shadows the tool;
  `lint-imports` imports contract types the checkout's config names.

Other languages add `zig build`, `mix credo`, `go`, `dotnet`, `clang-tidy`,
`cppcheck`, and generic tool specs. `scan` cannot switch these off.

## Scope

**Process-creation guard** (new module `desloppify/base/process_guard.py`).
`deny_process_creation()` is a context manager. On first use it installs one
`sys.addaudithook` hook; the hook acts only while a deny context is open, and
then raises `ExternalToolRefused` (a `PermissionError`) for the audit events
`subprocess.Popen`, `os.system`, `os.exec`, `os.posix_spawn`, `os.spawn`,
`pty.spawn`, and `os.startfile`, and counts the refusal. CPython raises these
events before the program is started, so a refused call runs nothing.
`refusal_count()` returns the counter and `process_creation_denied()` whether a
context is open. `os.fork` is not refused: a fork runs only desloppify's own
code, and its exec calls are refused in the child by the same hook.

**`scan --no-external-tools`** (`parser_groups.py`, `scan/cmd.py`). The whole
scan command runs inside `deny_process_creation()`. The flag is off by default;
an interactive scan without it behaves exactly as today.

**Phase runner** (`engine/planning/scan.py`). When `process_creation_denied()`:

- `prewarm_review_phase_detectors` is not called, so every tool attempt happens
  inside the phase that makes it (prefetch threads would otherwise overlap other
  phases). The phases already fall back to running synchronously.
- `_run_phases` reads `refusal_count()` before and after each phase. A phase
  whose count rose, or that raised after a refusal, is dropped: its issues and
  potentials are discarded and stderr prints
  `  [i/n] <label>... skipped: needs an external tool`. An exception with no
  refusal propagates as today.

Dropping potentials is what keeps state honest: `merge_scan_results` treats a
detector absent from potentials as not run, so its open findings are neither
added nor auto-resolved (`find_suspect_detectors`). There is no phase
classification list: a phase is dropped because it tried, so a new or
unclassified phase cannot start a tool in this mode.

A refused call outside any phase (language setup, post-scan steps) either is
handled by its caller as a missing tool or fails the scan; `_refresh` then parks
as `refresh-failed`. Both outcomes start nothing.

**Refresh** (`repair_cycle.py`). `_refresh` adds `--no-external-tools` to its
argv. Nothing else in that module changes.

**Selection.** `repair_queue.classify` routes only `concerns` and `dupes`;
every other detector is `ineligible` ("detector has no repair route"). `dupes`
comes from the in-process Duplicates phase, and `concerns` from review import,
not from the scan. So no selectable candidate depends on a dropped phase. If a
`dupes` phase were ever dropped, its open findings stay as they were and a
candidate is still rechecked against the current source by the evidence check
(ADR 0012) before publication.

**Guide** (`docs/systemd/repair-cycle.md`). Replace the sentence saying the
refresh's external tools still run (#75) with: the refresh scan starts no
external program; phases that need one (named by kind) are skipped and logged,
so the cycle state does not track their findings; interactive scans still run
them.

Out of scope: sandboxing interactive scans (operator); project plugins and the
checkout's git configuration (#63).

## Failure model

1. Actors and deployments
   - The `mending` service account running `repair-cycle` from the systemd unit
     against one checkout the host (and anyone who can merge to its default
     branch) can write.
   - An operator running `desloppify scan` interactively (behaviour unchanged).
2. Invariants and assets at stake
   - No program other than the running interpreter starts during a
     `--no-external-tools` scan (the service account's credentials and files).
   - Open findings of a dropped detector are not auto-resolved or invented.
   - Interactive scan output and state are unchanged without the flag.
3. Accepted failure classes
   - Fewer findings in the cycle state for tool-backed detectors (security,
     unused, smells, lints, boilerplate duplication): none has a repair route.
   - A checkout whose scan cannot complete without a tool parks every run as
     `refresh-failed`: visible, starts nothing.
   - Process creation the audit hook does not see (`ctypes` calls into libc,
     `_posixsubprocess` used directly by `multiprocessing` to re-exec the same
     interpreter): desloppify makes no such call to start a tool.
   - In-process parsing of checkout files (tree-sitter, `ast`) executes no
     checkout code.
4. Covered elsewhere
   - Checkout plugins and git configuration: #63 (`DESLOPPIFY_DENY_PLUGINS`,
     `_bring_current`).
   - Interpreter path shadowing of desloppify itself: `-P` in `_refresh`.

## Threat model

- Boundary widened: checkout content (manifests, build scripts, linter configs,
  `node_modules`) reaching program execution during the refresh. Added: none.
- Actor: whoever can land a commit on the checkout's default branch, or the
  host writing files into the checkout between runs. Trust stays with the
  installed desloppify package and the interpreter.
- Control: the audit hook refuses process creation for the scan's lifetime;
  the phase runner drops what tried. On failure it leaks only the phase label
  and the audit event name to the journal.
- Out of scope: denial of service through huge files (runtime limit already
  applies), interactive scans.

## Success

1. A `--no-external-tools` scan of a Rust fixture whose `build.rs` writes a
   marker, and of a JavaScript fixture whose `eslint.config.js` writes a marker,
   creates neither marker; a PATH shim for `cargo`, `npx`, and `node` records
   no call.
2. The same fixtures scanned without the flag call the shim (the test bites);
   a real-binary arm runs only when `cargo` / `npx` are installed.
3. In the mode, a phase that attempts a process is reported as skipped and its
   detector keeps its previous open findings; an in-process phase (Duplicates)
   still reports.
4. `_refresh` passes `--no-external-tools`.
5. Without the flag, the scan installs no hook and process creation is allowed.

## Validation

- `process_guard`: unit tests refuse `subprocess.run` and `os.system` inside
  the context, allow them outside, count refusals.
- Phase runner: unit test with a spawning phase and an in-process phase.
- Merge: a state with an open finding of a dropped detector keeps it open.
- Marker fixtures in `tmp_path` run the scan as a subprocess, as `_refresh` does.
- Existing `_refresh` argv test updated.
