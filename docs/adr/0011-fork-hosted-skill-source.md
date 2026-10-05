# 0011 — Download the agent skill from this repository's main branch

## Status

Accepted (2026-10-05)

## Context

`desloppify update-skill` downloads `docs/SKILL.md` and one host overlay from
`raw.githubusercontent.com/peteromallet/desloppify/main/docs`, the upstream
project. That skill tells agents to maximise the strict score and repeat until
the queue is empty, which contradicts ADR 0007: scores are optional
diagnostics, there are no finding quotas or score targets, and a no-op is a
successful run. `desloppify setup` already installs the copies bundled in
`desloppify/data/global/`, which `make sync-docs` keeps identical to `docs/`.
The operator decided on 2026-10-05 to keep the download and point it at this
repository rather than bundle (#38).

## Decision

- `update-skill` downloads from
  `https://raw.githubusercontent.com/randomparity/mending/main/docs`. The URL
  is a constant in `desloppify/app/commands/update_skill/cmd.py`; no
  configuration or environment variable overrides it.
- The ref is `main`, unpinned. The installed skill is whatever this
  repository's `main` holds at download time.
- `docs/SKILL.md` and its host overlays are this repository's documents and
  follow ADR 0007. Changes that agents should pick up bump `SKILL_VERSION` in
  `desloppify/app/skill_docs.py` and the `desloppify-skill-version` marker in
  `docs/SKILL.md` together.

## Consequences

- Trust: anyone who can push to this repository's `main` controls the text
  agents install with `update-skill`. That is the same party that controls the
  package source, so the download adds no new principal, but it does take
  effect without a release. The existing check that the payload carries a
  `desloppify-skill-version` marker is a format check, not an integrity check.
- A build older than `main` can install guidance written for a newer build.
  The command already prints the downloaded and expected versions; it does not
  refuse a mismatch, because refusing would break every older build after the
  next bump.
- `setup` (bundled copy) and `update-skill` (download) can differ for a given
  build; they agree on a build made from `main`.
- Only builds of this repository use this source; a build of the upstream
  project keeps its own URL.

## Considered & rejected

- **Bundle the skill and stop downloading.** judgment: fit; rejected by the
  operator decision of 2026-10-05 recorded on #38.
- **Pin to a commit SHA.** judgment: cost; a commit cannot name its own SHA,
  so every skill change needs a second commit to move the pin, and the pin
  still trusts whoever writes that commit.
- **Pin to a release tag.** verified: `git ls-remote --tags origin` returned no
  tags on 2026-10-05; a tag-pinned URL would name a ref that does not exist.
- **Keep the upstream source.** judgment: fit; it installs the score loop that
  ADR 0007 rules out, which is the defect #38 fixes.
