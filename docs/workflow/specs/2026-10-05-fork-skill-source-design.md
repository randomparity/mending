# Mending-aligned agent skill from this repository

## Problem

`desloppify update-skill` installs the upstream skill, which tells agents to
maximise the strict score and drain the queue. ADR 0007 and the README rule
both out. Issue #38. Decision: [ADR 0011](../../adr/0011-fork-hosted-skill-source.md).

## Scope

- `update_skill/cmd.py`: `_RAW_BASE` points at
  `randomparity/mending/main/docs`. No other behaviour changes.
- `docs/SKILL.md`, rewritten where it conflicts with ADR 0007:
  - "Your Job": bounded, verified work; findings are evidence, not a quota;
    the score is an optional diagnostic; a run that finds nothing worth fixing
    is a valid result; never skip or defer required tests to move a score.
    The "touch 20 files" instruction becomes: keep fixes small, and bring a
    change that needs a broad refactor to the user as a proposal.
  - Phase 1 drops "are we at target?"; Phase 3 replaces "grind the queue to
    completion", "repeat until the queue is empty", "keep going" after a score
    drop, and "when the queue is clear, go back to Phase 1" with: work the
    scope the user asked for, and stop when it is done or nothing left is
    worth fixing.
  - Scoring keeps the weights and score types; "north star", "even moderate
    scores dramatically improve" and "just follow the queue" go.
  - Section 4 stops telling agents to clone, PR, or file issues upstream on
    their own: they report a tool problem to the user, who may file it at
    `randomparity/mending`.
  - Prerequisite installs from `git+https://github.com/randomparity/mending.git`.
  - Version marker 7 → 8.
- Overlays: `docs/OPENCODE.md` drops "health score questions" from its trigger
  line. The other overlays hold review-batching mechanics only (excluded).
- `SKILL_VERSION` 7 → 8; `make sync-docs` refreshes `desloppify/data/global/`.
- README "Using the analyzer directly" note and the #16 spec deferral record
  the new source.

Unchanged: `setup` (reads the bundled copy), section-replacement and
frontmatter logic, review/triage/plan command reference, overlay review
mechanics, `docs/scoring.md`, runtime CLI strings.

### Failure model

- Actors and deployments: operators running `update-skill` or `setup`; agents
  following the installed skill; pushers to this repository's `main`.
- Invariants and assets: the installed skill asks for no score target, quota,
  or queue drain; agents publish nothing outside the repository unprompted;
  docs and bundled copies are byte-identical; marker equals `SKILL_VERSION`.
- Accepted failure classes: a build older than `main` installs newer guidance
  (ADR 0011 consequence); a push to `main` changes installed guidance without
  a release (ADR 0011 trust trade-off); runtime CLI strings outside the skill
  still mention score maximising (reported as follow-ups).
- Covered elsewhere: Adept skills and host adapter content (#19).

## Success

1. `_download("SKILL.md")` requests
   `https://raw.githubusercontent.com/randomparity/mending/main/docs/SKILL.md`
   (unit test with a mocked `urlopen`).
2. `docs/SKILL.md` names no score target, queue drain, or upstream repository;
   a test guards those phrases against an upstream re-import.
3. The bundled `SKILL.md` marker equals `SKILL_VERSION` (new test) and every
   bundled doc equals its `docs/` copy (existing test).
4. Guardrails: `make lint typecheck arch ci-contracts tests tests-full
   package-smoke` and the ADR records gate pass.
