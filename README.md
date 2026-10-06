# Desloppify - an agent harness to make your codebase 🤌

[![PyPI version](https://img.shields.io/pypi/v/desloppify)](https://pypi.org/project/desloppify/) ![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)

Desloppify gives your AI coding agent the tools to identify, understand, and systematically improve codebase quality. It combines mechanical detection (dead code, duplication, complexity) with subjective LLM review (naming, abstractions, module boundaries), then helps prioritize and track fixes. State persists across scans so it chips away over multiple sessions, and the scoring is designed to resist gaming.

<img src="assets/explained.png" width="100%">

The analyzer also produces a score, which can generate a scorecard badge for your GitHub profile or README:

<img src="assets/scorecard.png" width="100%">

Currently supports 29 languages — full plugin depth for TypeScript, Python, C#, C++, Dart, GDScript, Go, and Rust; generic linter + tree-sitter support for Ruby, Java, Kotlin, and 18 more. For C++ projects, `compile_commands.json` is the primary analysis path and `Makefile` repositories fall back to best-effort local include scanning.

## Mending maintenance workflow

Mending uses this analyzer as evidence for a bounded, host-driven maintenance
loop, defined in [ADR 0007](docs/adr/0007-host-driven-maintenance.md). The goal
is worthwhile, verified work, not a higher score. The contract, most of which
is still being built (see Planned below):

- Mending owns scan history, concern identity, dismissals, source-bound
  revalidation, candidate selection, and durable repair-attempt state.
- One concrete coding host runs the installed Adept skills for approved scope,
  claims, worktree isolation, implementation, verification, and review.
- GitHub holds actionable issues, human decisions, and PR links; Mending
  reconciles them with its local history.

A run ends with a verified, reviewed draft PR for one small repair, an
architecture proposal awaiting a human decision, a parked action, or a no-op.
A no-op is a successful result. Scores are optional diagnostics: there are no
finding quotas or score targets, and a score is never a reason to defer
required tests. On the host-driven path, discovery, publication, repair
execution, and merge are separately controlled; without explicit operator
opt-in for the repository, or without host and budget enforcement, that
action parks. Text in an issue, a model finding, or a proof string never
grants authority.

Available today, with the operator's flags as the only controls: `revalidate`
binds a concern to the git blob IDs of its concern file and related files at
`--revision` (default `HEAD`) of `--source-root` (default: the project root,
which must be the repository top level), and `sync --apply` publishes to
GitHub only while those files are unchanged, rechecking them before every
GitHub create and link write. `revalidate` also requires the concern's
evidence to cite those files as `PATH:LINE` and refuses unless every cited
line, and every identifier quoted in backticks beside it, is still there; `sync`
re-runs that check and drops a concern whose result changed. Concerns
revalidated before this check must be revalidated again.

Two routes reach the queue. A concern is a small repair when it has a
verification plan, high review confidence, and at most three files in one
directory; an exact duplicate pair of functions inside one file (`dupes`) is a
small repair too, proven by its line ranges and names. A concern that misses
any of those bounds becomes an architecture proposal: published without
`status:ready`, it asks for a human decision and is never dispatched. Each
`sync` first reconciles each eligible item's linked or pending issue,
including closed and human-edited ones, which are never recreated. It then publishes at most one
new small repair, ranked by fewer files, mechanical verification, more
source-checked evidence, and more cited lines, never by score. With `--apply`
it records the selection, or a no-op when nothing is safe or an earlier
create is still unresolved (a state with no work items records nothing). Linked issues store a reviewed-brief version that
moves only when evidence or a material brief field changes.

```bash
desloppify scan --path .          # refresh findings and scan history
desloppify review --run-batches   # bounded subjective and architecture-concern review
desloppify repair-queue revalidate ID --repo OWNER/REPO --apply
desloppify repair-queue sync --repo OWNER/REPO            # dry run; --apply publishes
desloppify repair-queue recover MARKER --repo OWNER/REPO --apply
desloppify repair-cycle --config FILE --state FILE        # parks: no host adapter yet
```

Planned, not yet available: a Mending-owned host adapter that runs one bounded repair ([#19](https://github.com/randomparity/mending/issues/19)).
A manual pilot ([#7](https://github.com/randomparity/mending/issues/7)) comes before any scheduled run; the
[systemd recipe](docs/systemd/repair-cycle.md) stays disabled until then.

## Using the analyzer directly

The rest of this README documents the inherited desloppify analyzer. Its agent
skill, installed by `update-skill` from this repository's `docs/`
([ADR 0011](docs/adr/0011-fork-hosted-skill-source.md)), covers the analyzer
commands and treats scores as diagnostics; it is not the Mending maintenance
lifecycle.

### Agent prompt

Paste this prompt into your agent:

```
I want you to improve the quality of this codebase. To do this, install and run desloppify.
Run ALL of the following (requires Python 3.11+):

pip install --upgrade "desloppify[full] @ git+https://github.com/randomparity/mending.git"
desloppify update-skill claude    # installs the full workflow guide — pick yours: claude, cursor, codex, copilot, droid, windsurf, gemini, rovodev

Add .desloppify/ to your .gitignore — it contains local state that shouldn't be committed.

Before scanning, check for directories that should be excluded (vendor, build output,
generated code, worktrees, etc.) and exclude obvious ones with `desloppify exclude <path>`.
Share any questionable candidates with me before excluding.

desloppify scan --path .
desloppify next

--path is the directory to scan (use "." for the whole project, or "src/" etc).

Treat the findings as evidence, not a quota. The score is a diagnostic, not a target, and
a clean result with nothing worth fixing is a valid outcome.

Run `next` to see the current item from the living plan's execution queue: what to fix,
which file, and the resolve command to run when done. Fix what is worth fixing and resolve it.

Use `desloppify backlog` only when you need to inspect broader open work that is not currently
driving execution.

Fix things properly. Never skip or defer required tests to move a score.

Use `plan` / `plan queue` to reorder priorities or cluster related issues. Rescan periodically.
The scan output includes agent instructions — follow them, don't substitute your own analysis.
```

## Monorepos and multi-project directories

If your workspace contains multiple programs (e.g., a frontend and backend in sibling directories), scan each one separately with `--path`:

```bash
desloppify --lang typescript scan --path ./frontend
desloppify --lang python scan --path ./backend
```

Scanning the parent directory that contains both will mix state and path context across unrelated codebases, producing unreliable results. Each `--path` target should be a single coherent project. Desloppify maintains separate state per language, so you can scan a TypeScript frontend and a Python backend from the same workspace without conflict — just target them individually.

## Development

Set up the complete locked development environment, including optional runtime
capabilities, with:

```bash
make setup
```

`make setup` uses a compatible `uv` already on your PATH or bootstraps a
checksummed repository-local copy. Run project checks through their Makefile
targets, for example `make tests`, `make lint`, or `make ci`.

## CI

Desloppify works best in CI as a full-codebase health gate, not as a diff-only linter. Run the CI profile against the same coherent project path you scan locally:

```bash
desloppify scan --path . --profile ci --no-badge
desloppify status --json
```

`--profile ci` skips slow and subjective phases and bypasses the mid-cycle scan queue gate so a CI job can collect a fresh mechanical snapshot. Use `status --json` if you want a script to read the strict/objective scores and enforce your own threshold.

On constrained Java CI runners, the PMD detector defaults to `--threads 0` to avoid worker-thread fanout. Set `DESLOPPIFY_PMD_THREADS` to a PMD thread value such as `2` or `0.5C` if you want more throughput.

Minimal GitHub Actions example:

```yaml
name: desloppify

on:
  pull_request:
  push:
    branches: [main]

jobs:
  health:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install --upgrade "desloppify[full]"
      - run: desloppify scan --path . --profile ci --no-badge
      - run: desloppify status --json
```

For monorepos, run one job or matrix entry per project path instead of scanning the workspace root. True incremental or diff-only scanning is not the supported model yet; compare full-codebase results across runs or enforce a project-level threshold.

## How it works

```
scan ──→ score ──→ review ──→ triage ──→ execute ──→ rescan
  │         │         │          │          │           │
  │     dimensions    │     prioritize    fix it     verify
  │     scored      LLM reviews  & cluster  & resolve  improvements
  │                 subjective   the queue
  │                 quality
  detectors find
  mechanical issues
  (dead code, smells,
  test gaps, etc.)
```

**Scan** runs mechanical detectors across your codebase — dead code, duplication, complexity, test coverage gaps, naming issues, and more. Each issue is scored by dimension (File health, Code quality, Test health, etc.).

**Review** uses an LLM to assess subjective quality dimensions — naming, abstractions, error handling patterns, module boundaries. These score alongside the mechanical dimensions.

**Triage** is where prioritization happens. The agent (or you) observes the findings, reflects on patterns, organizes issues into clusters, and enriches them with implementation detail. This produces an ordered execution queue — only items explicitly queued appear in `next`. Before triage, all mechanical issues are visible in the queue sorted by impact, which can be noisy.

**Execute** is the fix loop: `next` → fix → `resolve` → `next`. Items come from the triaged queue. Autofix handles what it can; the rest needs manual or agent work.

**Rescan** verifies improvements, catches cascading effects, and feeds the next cycle.

State persists in `.desloppify/` so progress carries across sessions. The scoring resists gaming — wontfix items widen the gap between lenient and strict scores, and re-reviewing dimensions can lower scores if the reviewer finds new issues.

## From Vibe Coding to Vibe Engineering

Vibe coding gets things built fast. But the codebases it produces tend to rot in ways that are hard to see and harder to fix — not just the mechanical stuff like dead imports, but the structural kind. Abstractions that made sense at first stop making sense. Naming drifts. Error handling is done three different ways. The codebase works, but working in it gets worse over time.

LLMs are actually good at spotting this now, if you ask them the right questions. That's the core bet here — that an agent with the right framework can hold a codebase to a real standard, the kind that used to require a senior engineer paying close attention over months.

So we're trying to define what "good" looks like as a score that's actually worth optimizing. Not a lint score you game to 100 by suppressing warnings. Something where improving the number means the codebase genuinely got better. That's hard, and we're not done, but the anti-gaming stuff matters to us a lot — it's the difference between a useful signal and a vanity metric.

The hope is that anyone can use this to build something a seasoned engineer would look at and respect. That's the bar we're aiming for.

If you'd like to join a community of vibe engineers who want to build beautiful things, [come hang out](https://discord.gg/aZdzbZrHaY).

<img src="assets/engineering.png" width="100%">

---

Issues, improvements, and PRs are hugely appreciated — [github.com/randomparity/mending](https://github.com/randomparity/mending).

Desloppify is free for any individual — whether working independently or at a company — to use for their own work. It is also free for open source companies to use in any capacity, including commercial. Non-open source companies who wish to commercialize it should refer to the [LICENSE](LICENSE) for transparent pricing details.
