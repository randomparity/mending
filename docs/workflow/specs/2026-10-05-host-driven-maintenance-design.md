# Host-driven maintenance contract

## Problem

ADR 0006 waits on an Adept-owned service that will not exist, and the README
teaches score maximization. Issue #16 needs an honest ownership contract and
entry path before #17–#19 build on it. Decision: [ADR 0007](../../adr/0007-host-driven-maintenance.md).

## Scope

Documentation only, no code: add ADR 0007; rewrite the README entry path; edit
the Adept prerequisite in `docs/systemd/repair-cycle.md`. Per-host overlays
(`docs/*.md`) describe the inherited analyzer loop; they change only where they
contradict the ownership contract. ADRs 0005/0006 stay unedited; ADR 0007
names the portions it supersedes. No ownership transition in code: the seam
map in ADR 0007 hands each seam to #17, #18, #19, or #7.

The README keeps the package name, install command, `[full]` extra, analyzer
docs, and CI guidance. It separates commands available today (`scan`,
`review`, `repair-queue`, `repair-cycle` that parks) from planned work.

### Failure model

- Actors and deployments: readers of README and the systemd guide; agents
  following the README.
- Invariants and assets: no doc claims a live repair path, host adapter, or
  timer readiness that does not exist; accepted ADRs keep their bodies.
- Accepted failure classes: inherited analyzer overlays still describe the
  analyzer's own score loop; tolerated because they document the analyzer,
  not the maintenance lifecycle, and the README says so.
- Covered elsewhere: adapter and loader #19; concern key and revalidation
  #17; briefs and proposals #18; opt-in, pilot, timer, merge authority #7.

## Success

1. ADR 0007 passes the records gate and names the superseded ADR 0005/0006
   portions.
2. ADR 0007 defines both output paths, the four separately controlled
   capabilities, retained safety properties, one active repair and at most one
   new dispatch per window, and merge outside the pilot.
3. README's first instructions are the Mending entry path: no-op success,
   scores as diagnostics, today versus planned commands.
4. The systemd guide names a Mending-owned host adapter (#19) and gates
   activation on the pilot and approved scope and limits (#7).

## Validation

- Records shape (1): `focused-test`; red: a draft missing
  `## Considered & rejected` fails `E-SECTION-MISSING`; green:
  `BASE_SHA=$(git merge-base origin/main HEAD) RECORD_PROFILES=adr .github/scripts/check-records.sh`.
- Install extras (3): `focused-test`;
  `pytest -q desloppify/tests/ci/test_ci_contracts.py -k readme`; red: an
  undefined extra in README.
- Guide parking wording (4): `focused-test`;
  `pytest -q desloppify/tests/ci/test_repair_cycle_recipe.py`; red: removing
  "parks before selection".
- Ownership and capability prose (2, 3, 4): `task-test-not-applicable`; no
  executable consumer reads these statements, so only the branch review can
  check them against the issue criteria.
