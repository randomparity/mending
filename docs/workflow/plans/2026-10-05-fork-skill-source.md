# Plan: Mending-aligned agent skill (#38)

Spec: [fork skill source](../specs/2026-10-05-fork-skill-source-design.md).
Branch `feat/fork-skill-source-38`, base `main`.

## Task 1 — download source

- Test first: in `desloppify/tests/commands/test_update_skill_cmd_direct.py`,
  patch `urllib.request.urlopen` and assert `_download("SKILL.md")` requests
  `https://raw.githubusercontent.com/randomparity/mending/main/docs/SKILL.md`.
  Red against the upstream URL.
- Change `_RAW_BASE` in `desloppify/app/commands/update_skill/cmd.py`.
- Verify: the new test passes.

## Task 2 — skill text and version

- Tests first, in `desloppify/tests/commands/test_bundled_sync.py`: the bundled
  `SKILL.md` marker equals `SKILL_VERSION`; `docs/SKILL.md` contains none of
  `peteromallet`, `queue is empty`, `north star`, `Maximise the`. Red on the
  current text.
- Rewrite `docs/SKILL.md` per the spec; marker 8. Edit `docs/OPENCODE.md`
  line 3. Set `SKILL_VERSION = 8`. Run `make sync-docs`.
- Verify: `test_bundled_sync.py`, `test_setup.py`, update-skill tests pass.

## Task 3 — records

- README "Using the analyzer directly" note; #16 spec deferral paragraph and
  accepted-failure line point at #38 and ADR 0011.
- Verify: records gate, `rg -n "peteromallet" docs/SKILL.md desloppify/data/global/SKILL.md`
  returns nothing.

## Final

`make lint typecheck arch ci-contracts tests tests-full package-smoke`, then
`BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
