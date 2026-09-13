# GitHub repair-queue promotion

Promote complete, current architectural concerns to one recoverable GitHub
issue each. A small `gh` adapter owns external I/O; state locking owns durable
local links; the command owns dry-run/apply selection. Python 3.11+ and the
locked `uv` toolchain apply.

## Global Constraints

- `gh` is the only new transport; do not add an HTTP dependency.
- GitHub writes require explicit `--apply`; every uncertainty fails closed.
- Do not implement Adept claims, execution, scheduling, discovery, or pilot work.
- Public payloads exclude raw evidence, paths, and unvalidated free-form text.

Expected implementation size: 520–650 changed lines — parser/command wiring,
deterministic marker and payload rendering, injected transport, locked state persistence,
and focused transport fixtures. The campaign denominator remains the fixed 250 (M);
this estimate records variance and does not change that denominator.

## Task 1: Define promotion records and public rendering

Files: `desloppify/engine/repair_queue.py`,
`desloppify/tests/repair_queue/test_promotion.py`.

Interfaces: `PromotionCandidate.from_issue(issue: Mapping[str, object]) ->
PromotionCandidate | None` accepts only an open `concerns` item with nonempty
`concern_identity` and `concern_evidence_digest` and without
`previous_concern_identity` or `previous_concern_evidence_digest`. `marker` is
SHA-256 over the two hashes. `render_issue()` returns title/body built only from
the marker, evidence digest, and normalized allowlisted detail fields.

Verification:

- Mode: focused-test. Contract: incomplete, stale, and unsafe candidates are
  rejected; equivalent candidates produce the same marker and public body.
  Red observation: no promotion model exists. Green command:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_promotion.py`
  exits 0.

Steps: write fixtures for complete and invalid concern detail; assert red;
implement the frozen candidate/marker/public-rendering contract; assert green.

Acceptance: source-only fields cannot enter the rendered payload.

## Task 2: Add an injected `gh` transport and reconciliation service

Files: `desloppify/engine/repair_queue.py`,
`desloppify/tests/repair_queue/test_promotion.py`.

Interfaces: `GitHubIssueClient.run(argv: Sequence[str]) -> CompletedProcess[str]`
is injected into `sync_candidates`. The service searches with the fixed
`gh issue list --state all --search MARKER --limit 100 --json number,url`
argument shape, adopts exactly one `{number, url}`, creates only after an empty
search, then re-searches for exactly one result. It returns `created`,
`adopted`, `skipped`, or `uncertain` records and never constructs a shell command
from concern text.

Verification:

- Mode: focused-test. Contract: open/closed match adoption, zero-match create,
  duplicate-match, malformed JSON, process error, and lost-create-response
  paths make the stated result and never issue an unsafe second create. Red
  observation: no adapter exists. Green command:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_promotion.py`
  exits 0.

Steps: add fake process fixtures; prove the failure cases red; implement fixed
argument lists and result decoding; prove each recovery disposition green.

Acceptance: only a successful, unambiguous response permits a state link.

## Task 3: Wire `repair-queue sync` through parser, command, and state lock

Files: `desloppify/app/cli_support/parser.py`,
`desloppify/app/cli_support/parser_groups.py`,
`desloppify/app/cli_support/parser_groups_repair_queue.py`,
`desloppify/app/commands/repair_queue.py`,
`desloppify/app/commands/registry.py`, `desloppify/engine/repair_queue.py`,
`desloppify/tests/commands/test_repair_queue.py`.

Interfaces: `desloppify repair-queue sync [--apply] [--state PATH]` defaults to
dry-run. The apply route enters `state_lock(path)`, reloads candidates inside the
lock, calls the service, and saves only unambiguous links under
`issue["detail"]["github_repair"]` with `number`, `url`, and `marker`. Dry run
does not enter the state lock or invoke create.

Verification:

- Mode: focused-test. Contract: parser routing, dry-run non-mutation, apply
  persistence, and stale concern skipping are observable under a fake client.
  Red observation: the command is absent. Green command:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_queue.py`
  exits 0.

Steps: add parser and registry entries; write red command fixtures; implement
the one-shot handler and precise output; run focused tests green; run the
repository guardrails.

Acceptance: the command never schedules, claims, or executes a repair.
