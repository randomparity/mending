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
`previous_concern_identity` or `previous_concern_evidence_digest`. Their absence
after a later unchanged scan proves the #4 revalidation gate; it does not consult
historical plan supersession entries. `marker` is SHA-256 over the two hashes.
`render_issue()` returns title/body built only from the marker, evidence digest,
and required detail fields that are one-to-eight-word lower-case phrases matching
ADR-0005's word grammar and structural/denylist checks. A failed field rejects
the candidate; no raw source string is redacted into public output.

Verification:

- Mode: focused-test. Contract: incomplete, stale, and unsafe candidates are
  rejected; equivalent candidates produce the same marker and public body.
  Fixtures cover email, hostname, IPv4, path, URL, secret-like, prompt-like,
  opaque-token, and out-of-vocabulary source text.
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
search, then rechecks under the state lock and persists `PendingPromotion(marker)`
in a completed transaction before its first create. Pending candidates only
search/adopt on later runs. It returns `created`, `adopted`, `pending`, `skipped`,
or `uncertain` records and never constructs a shell command from concern text.

Verification:

- Mode: focused-test. Contract: open/closed match adoption, zero-match create,
  duplicate-match, malformed JSON, process error, and lost-create-response
  paths make the stated result. A crash after the pending transaction forbids a
  second create; a pending marker remains until attested recovery clears it.
  Red observation: no adapter exists. Green command:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_promotion.py`
  exits 0.

Steps: add fake process fixtures; prove the failure cases red; implement fixed
argument lists and result decoding; prove each recovery disposition green.

Acceptance: only a successful, unambiguous response permits a state link, and
there is no automatic retry of an uncertain create.

## Task 3: Wire `repair-queue sync` through parser, command, and state lock

Files: `desloppify/app/cli_support/parser.py`,
`desloppify/app/cli_support/parser_groups.py`,
`desloppify/app/cli_support/parser_groups_repair_queue.py`,
`desloppify/app/commands/repair_queue.py`,
`desloppify/app/commands/registry.py`, `desloppify/engine/repair_queue.py`,
`desloppify/engine/_state/merge_issues.py`,
`desloppify/tests/commands/test_repair_queue.py`.

Interfaces: `desloppify repair-queue sync [--apply] [--state PATH]` defaults to
dry-run. The apply route rechecks candidates inside `state_lock(path)`, commits
`github_repair_pending` before a first create, then performs external I/O after
the lock transaction. A later lock transaction saves only unambiguous links
under `issue["detail"]["github_repair"]` with `number`, `url`, and `marker`.
`repair-queue recover
MARKER --apply --attest TEXT` clears only the matching pending marker after
explicit operator attestation. State merging preserves a matching repair link or
pending marker and drops it when concern hashes change. Dry run does not enter
the state lock or invoke create.

Verification:

- Mode: focused-test. Contract: parser routing, dry-run non-mutation, apply
  persistence, crash-safe pending creation, pending recovery, metadata
  preservation across an unchanged scan, and stale concern skipping are
  observable under a fake client.
  Red observation: the command is absent. Green command:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_queue.py`
  exits 0.

Steps: add parser and registry entries; write red command fixtures; implement
the one-shot handler and precise output; run focused tests green; run the
repository guardrails.

Acceptance: the command never schedules, claims, executes a repair, or promises
serialization across independent state files.
