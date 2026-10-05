# Host-driven maintenance contract

## Problem

ADR 0006 waits on an Adept-owned service that will not exist, and the README
teaches score maximization. Issue #16 needs an honest ownership contract and
entry path before #17–#19 build on it. Decision: [ADR 0007](../../adr/0007-host-driven-maintenance.md).

## Scope

Documentation only, no code: add ADR 0007; rewrite the README entry path; edit
the Adept prerequisite in `docs/systemd/repair-cycle.md`. ADRs 0005/0006 stay
unedited. The seam map in ADR 0007 hands each code seam to #17, #18, #19, or #7.

The README keeps the package name, install command, `[full]` extra, analyzer
docs, and CI guidance. It separates commands available today (`scan`,
`review`, `repair-queue`, `repair-cycle` that parks) from planned work.
`docs/SKILL.md` and the per-host overlays stay unchanged: `update-skill`
downloads them from the upstream repository (`_RAW_BASE` in
`desloppify/app/commands/update_skill/cmd.py`), so editing them here would not
change what agents install. #38 later pointed `update-skill` at this
repository and aligned the skill
([ADR 0011](../../adr/0011-fork-hosted-skill-source.md)).

### Failure model

- Actors and deployments: readers of README and the systemd guide; agents
  following the README.
- Invariants and assets: no doc claims a live repair path, host adapter, or
  timer readiness that does not exist; accepted ADRs keep their bodies.
- Accepted failure classes: the upstream analyzer skill still teaches its
  score loop; tolerated because the README labels it as the inherited
  analyzer workflow, not the maintenance entry path (Success 3). Resolved by
  #38.
- Covered elsewhere: adapter and loader #19; concern key and revalidation
  #17; briefs and proposals #18; opt-in, pilot, timer, merge authority #7.

## Success

1. ADR 0007 passes the records gate and names the replaced ADR 0005/0006
   portions.
2. ADR 0007 defines both output paths, the four separately controlled
   capabilities with owners, accepted safety properties, one active repair and
   at most one new dispatch per window, and merge outside the pilot.
3. README's first instructions are the Mending entry path: no-op success,
   scores as diagnostics, today versus planned commands; `update-skill` and
   the score loop appear only under the labeled inherited analyzer section.
4. The systemd guide names a Mending-owned host adapter (#19) and gates
   activation on the pilot and approved scope and limits (#7).
5. README, the systemd guide, and ADR 0007 make no contradictory ownership or
   live-readiness claim.

## Validation

- Records shape (1): `focused-test`; red: a draft missing
  `## Considered & rejected` fails `E-SECTION-MISSING`; green:
  `BASE_SHA=$(git merge-base origin/main HEAD) RECORD_PROFILES=adr .github/scripts/check-records.sh`.
- Install extras (3): `focused-test`; red: an undefined extra in README;
  `pytest -q desloppify/tests/ci/test_ci_contracts.py -k readme`.
- Retained parking wording (regression guard only): `pytest -q
  desloppify/tests/ci/test_repair_cycle_recipe.py`.
- Ownership, capability, and gating prose (2–5): `task-test-not-applicable`; no
  executable consumer reads these statements, so only branch review checks them.
