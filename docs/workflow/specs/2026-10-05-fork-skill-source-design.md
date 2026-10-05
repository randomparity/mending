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
  - Phase 1 drops "are we at target?" and "don't ask the user what to do.
    Just run `next` and follow"; Phase 3 replaces "grind the queue to
    completion", "finish the queue first", "repeat until the queue is empty",
    "keep going" after a score drop, and "when the queue is clear, go back to
    Phase 1" with: work the scope the user asked for, and stop when it is
    done or nothing left is worth fixing.
  - Scoring keeps the weights and score types; "north star", "even moderate
    scores dramatically improve" and "just follow the queue" go.
  - Section 4 stops telling agents to clone, PR, or file issues upstream on
    their own: they report a tool problem to the user, who may file it at
    `randomparity/mending`.
  - Prerequisite: both the `uvx` line and the `pip` fallback install from
    `git+https://github.com/randomparity/mending.git`; a bare
    `pip install desloppify[full]` installs the upstream package, whose
    `setup` and `update-skill` install the upstream skill.
  - Version marker 7 → 8.
- Overlays: `docs/OPENCODE.md` drops "health score questions" from its trigger
  line. The other overlays hold review-batching mechanics only (excluded).
- Scan agent guide (`scan/reporting/text.py`): the skill now points agents at
  the scan's instructions, so the guide's "outer loop … at target?" and "inner
  loop … repeat until plan clear" sentence becomes the bounded-run sentence.
  The operator's exclusion of scan/review agent strings excepts strings that
  say drain the queue; this is the only one found. The guide's other score
  wording stays (excluded).
- `SKILL_VERSION` 7 → 8; `make sync-docs` refreshes `desloppify/data/global/`.
- README "Using the analyzer directly" note and the #16 spec deferral record
  the new source.

Unchanged: `setup` (reads the bundled copy), section-replacement and
frontmatter logic, review/triage/plan command reference, overlay review
mechanics, `docs/scoring.md`, runtime CLI strings other than the drain
sentence above.

### Failure model

- Actors and deployments: operators running `update-skill` or `setup`; agents
  following the installed skill; pushers to this repository's `main`.
- Invariants and assets: the installed skill asks for no score target, quota,
  or queue drain; agents publish nothing outside the repository unprompted;
  docs and bundled copies are byte-identical; marker equals `SKILL_VERSION`.
- Accepted failure classes: a build older than `main` installs newer guidance
  (ADR 0011 consequence); a push to `main` changes installed guidance without
  a release (ADR 0011 trust trade-off); upstream and this repository share a
  version space (ADR 0011). Runtime agent prompts outside the installed skill
  are outside this change's surface and reported as follow-up candidates:
  the Hermes autoreply prompt ("keep going until the queue is empty",
  `desloppify/app/commands/helpers/transition_messages.py`), the scan reminder
  ("The goal is to maximize strict scores",
  `desloppify/intelligence/narrative/reminders_rules_followup.py`), and the
  status hint ("it's your north star", `desloppify/app/commands/status/summary.py`),
  the scan score guide and LLM header ("your north star", "The goal is to
  maximize strict scores", `scan/reporting/summary.py`, `agent_context.py`),
  and the `next` nudge ("North star: strict … target",
  `next/render_nudges.py`).
- Covered elsewhere: Adept skills and host adapter content (#19).

## Success

1. `_download("SKILL.md")` requests
   `https://raw.githubusercontent.com/randomparity/mending/main/docs/SKILL.md`
   (unit test with a mocked `urlopen`).
2. `docs/SKILL.md` names no score target, queue drain, or upstream repository
   or package; a test guards the removed upstream phrases against a re-import.
3. The bundled `SKILL.md` marker equals `SKILL_VERSION` (new test) and every
   bundled doc equals its `docs/` copy (existing test).
4. `build_workflow_guide` contains no "repeat until plan clear" or "at
   target?" (new test).
5. Guardrails: `make lint typecheck arch ci-contracts tests tests-full
   package-smoke` and the ADR records gate pass.
