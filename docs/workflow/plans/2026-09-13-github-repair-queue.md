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

Expected implementation size: 650–800 changed lines — parser/command wiring,
deterministic marker and payload rendering, injected transport, locked state persistence,
and focused transport fixtures. The implementation is 776 code/test lines: the
pending-create transaction, explicit repository verification, stale-result check,
matching scan-metadata preservation, atomic `status:ready` creation, revalidation
attestation validation, and closed-match state readback are required recovery
contracts surfaced by independent review. The campaign denominator remains the fixed
250 (M); this estimate records variance and does not change that denominator.

## Task 1: Define promotion records and public rendering

Files: `desloppify/engine/repair_queue.py`,
`desloppify/tests/repair_queue/test_promotion.py`.

Interfaces: `PromotionCandidate.from_issue(issue: Mapping[str, object]) ->
PromotionCandidate | None` accepts only an open `concerns` item with nonempty
`concern_identity` and `concern_evidence_digest` and without
`previous_concern_identity` or `previous_concern_evidence_digest`. Their absence
is necessary but not sufficient: `github_repair_revalidated` must contain the
same marker and an explicit attestation. `marker` is SHA-256 over the two hashes.
`render_issue()` returns title/body built only from static labels and the marker,
identity digest, and evidence digest; it never renders source text.

Verification:

- Mode: focused-test. Contract: incomplete, stale, and un-revalidated candidates
  are rejected; equivalent candidates produce the same marker and source-free
  public body. Fixtures prove that hostile source text cannot enter the body.
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
is injected into `sync_candidates`. Every fixed argv includes the explicit
`--repo OWNER/REPO` value. The service searches with the fixed `gh issue list
--state all --search MARKER --limit 100 --json number,url,state` argument shape. A
matching existing link uses the fixed `gh issue view NUMBER --json number,url,state`
form and is read back even if its body marker was edited. Otherwise it adopts
exactly one `{number, url}`, creates only after an empty search, then rechecks
under the state lock and persists
`PendingPromotion(marker)` in a completed transaction before its first create.
Pending candidates only search/adopt on later runs. It returns `created`,
`adopted`, `reconciled`, `pending`, `skipped`, or `uncertain` records and never
constructs a shell command from concern text.

`GitHubIssueClient.resolve_repository(repository: str)` invokes `gh repo view`
with that explicit repository and accepts only an exact `nameWithOwner` match
before revalidation persists it.

Verification:

- Mode: focused-test. Contract: open/closed match adoption, zero-match create,
  duplicate-match, malformed JSON, process error, human-edited closed link,
  repository mismatch, stale post-create marker, and lost-create-response paths
  make the stated result. A crash after the pending transaction forbids a second
  create; a pending marker remains until attested recovery clears it. Red
  observation: no adapter exists. Green command:
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

Interfaces: `desloppify repair-queue revalidate ID --repo OWNER/REPO --apply
--attest TEXT` first resolves the exact repository identity, then writes the
explicit matching revalidation attestation.
`desloppify repair-queue sync --repo OWNER/REPO [--apply] [--state PATH]`
defaults to dry-run. The apply route rechecks candidates inside `state_lock(path)`,
commits `github_repair_pending` with the repository before a first create, then
performs external I/O after the lock transaction. A later lock transaction saves
only an unambiguous link whose current hashes, pending marker, and repository
still match, under `issue["detail"]["github_repair"]` with `number`, `url`,
`state`, `marker`, and `repository`. `repair-queue recover MARKER --repo
OWNER/REPO --apply --attest TEXT` clears only a matching pending marker after
explicit operator attestation. State merging preserves matching repair metadata
and drops it when concern hashes change. Dry run does not enter the state lock or
invoke create.

Verification:

- Mode: focused-test. Contract: parser routing, dry-run non-mutation, apply
  revalidation attestation, persistence, crash-safe pending creation, pending
  recovery, repository binding, stale-result rejection, metadata preservation,
  and linked-issue state readback across an unchanged scan are observable under
  a fake client.
  Red observation: the command is absent. Green command:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_queue.py`
  exits 0.

Steps: add parser and registry entries; write red command fixtures; implement
the one-shot handler and precise output; run focused tests green; run the
repository guardrails.

Acceptance: the command never schedules, claims, executes a repair, or promises
serialization across independent state files.
