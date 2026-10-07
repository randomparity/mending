# 0017 — Refuse process creation in the repair-cycle refresh scan

## Status

Accepted (2026-10-07)

## Context

The repair cycle (ADR 0006, ADR 0016) refreshes by running `desloppify scan`
in a checkout that anyone able to land on its default branch, or the host,
can write. The scan's phases start external programs there — `cargo`,
`npx eslint`, `npx --yes jscpd`, `python -m bandit`, `lint-imports`, and
others — several of which execute checkout-controlled code (#75). The refresh
must start none of them, interactive scans must keep running them, and the
cycle state must not mistake a skipped detector for one that found nothing.

## Decision

- `scan --no-external-tools` runs the whole command under a process-creation
  guard: a `sys.addaudithook` hook that, while the guard is open, raises
  `ExternalToolRefused` (a `PermissionError`) on the audit events CPython
  emits before starting a program (`subprocess.Popen`, `os.system`,
  `os.exec`, `os.posix_spawn`, `os.spawn`, `os.fork`, `os.forkpty`,
  `pty.spawn`, `os.startfile`), and counts each refusal. `os.fork` is in the
  list because `os.spawn*` forks first and would exec, and be refused, only in
  the child, out of the parent's count.
- Under the guard the phase runner skips the background prefetch and drops
  any phase during which a refusal occurred: its issues and potentials are
  discarded, its writes to the persisted detector caches (`review_cache`) and
  coverage records are undone, and the skip is printed. A detector absent from
  potentials is treated by merge as not run, so its open findings are kept, not
  resolved; undoing the cache writes keeps a later run over unchanged files
  from reporting the refusal-degraded result as a clean one.
- A refusal outside a phase is left to its caller: handled as a missing tool,
  or it fails the scan, which the refresh parks as `refresh-failed`.
- `_refresh` passes the flag. Without it nothing changes.

## Consequences

The refresh state no longer tracks tool-backed detectors (lints, security,
unused imports, smells, boilerplate duplication). None has a repair route;
selection routes only `concerns` and `dupes`, which the refresh still
produces. A phase that mixes in-process and tool checks is dropped whole, so
its in-process findings are lost in this mode too. A future phase needs no
registration to be covered. The guard sees only process creation that goes
through audited calls; a direct `ctypes` call into libc would bypass it, and
desloppify makes none. The hook stays installed for the life of the process
but acts only while a guard is open.

## Considered & rejected

- **Allow-list in-process phases.** verified: `rg -c "DetectorPhase\("
  desloppify/languages` at `e36caa07` counts 44 constructions in 15 files
  across 30 language packages (`find desloppify/languages -mindepth 1 -maxdepth 1 -type d ! -name "_*"`), plus shared builders; every one would need a
  mark, and the jscpd prefetch that starts `npx` runs outside any phase.
  judgment: a mark is a claim nobody re-checks when a phase gains a tool.
- **Deny-list tool phases.** verified: tracing a Python fixture at `e36caa07`
  showed `python -m bandit` started during the phase labelled `Unused (ruff)`
  and `npx --yes jscpd` before the first phase; a label- or factory-based list
  misses both, and a missed entry runs the tool.
- **Empty `PATH` or restrict exec in the systemd unit.** verified: the trace
  above shows `python -m bandit` through `sys.executable`, which must stay
  executable, `cargo rustdoc` through `/bin/sh -lc`, and `npx` resolving the
  checkout's `node_modules`; an exec allow-list that keeps the interpreter and
  shell still runs them. judgment: it protects only the unit, not a manual
  `repair-cycle` run.
- **Park any checkout whose languages have tool phases.** judgment: fit;
  Python, the main target, has tool phases, so every refresh would park.
- **Do nothing; document the risk.** judgment: the guide already did (#63);
  #75 exists because a commit to the default branch runs code as the service
  account before any authority check.
