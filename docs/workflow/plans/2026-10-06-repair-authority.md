# Implement trusted repair-cycle authority

Goal: replace the client-supplied authority proof with an operator approval
from the trusted cycle configuration, checked at selection, before dispatch,
and on resume. Architecture: a pure engine module decodes and checks
approvals; `repair-cycle` builds the binding from the current work item,
reuses repair-queue's source comparison, and records the bound revision on
the attempt. Spec: `docs/workflow/specs/2026-10-06-repair-authority-design.md`;
ADR 0015. Stack: Python 3.11+, pytest, mypy, ruff, import-linter.

Expected implementation size: 850–1000 changed lines (M) — engine module
(~195), command wiring (~240), state and disposal (~45), parser and docs
(~55), tests (~460, mostly fixtures every existing case now needs); above the
M band because the shared fixtures change, not because scope grew.

## Global Constraints

- No new dependency. Decimal for money; timezone-aware datetimes only.
- Park reasons are exactly the strings in ADR 0015.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and the records gate
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
- Focused command: `uv run --locked pytest desloppify/tests/commands/test_repair_cycle.py desloppify/tests/commands/test_repair_queue.py -q`.

## File map

- `desloppify/engine/repair_authority.py` (new) — owns approval decoding and
  `check_authority` (criteria 1–4).
- `desloppify/engine/repair_cycle.py` — `CycleConfig.authority`,
  `CycleState.authority`, `dispose(confirm_stopped=)` (criteria 3, 5).
- `desloppify/app/commands/repair_queue.py` — `_comparison` renamed
  `source_comparison` (public, both callers updated) for reuse (criterion 6).
- `desloppify/app/commands/repair_cycle.py` — `_authorize` and its three call
  sites; removes `AuthorityProof`, `_valid_authority`, and
  `AdeptCycleClient.verify_authority` (obsolete path); `client.select`
  receives the binding.
- `desloppify/app/cli_support/parser_groups_repair_cycle.py` — `--confirm-stopped`.
- `desloppify/tests/commands/test_repair_cycle.py` — fixtures and cases.
- `docs/systemd/repair-cycle.md` — authority block and disposal.

## Task 1 — Engine authority check

Files: create `desloppify/engine/repair_authority.py`; test in
`desloppify/tests/commands/test_repair_cycle.py`.

Interfaces (later tasks rely on these exact names):

```python
SUPPORTED_ACTIONS = frozenset({"repair"})
class UnsupportedAuthority(ValueError): ...
@dataclass(frozen=True)
class Approval: key: str; reviewed_brief_version: str; evidence_digest: str
    files: frozenset[str]; actions: frozenset[str]; call_limit: int
    cost_cap_usd: Decimal; runtime_minutes: int; expires_at: datetime; revoked: bool
@dataclass(frozen=True)
class Authority: repository: str; revision: str; approvals: tuple[Approval, ...]
@dataclass(frozen=True)
class AuthorityBinding: repository: str; key: str; reviewed_brief_version: str
    evidence_digest: str; files: frozenset[str]; action: str; call_limit: int
    cost_cap_usd: Decimal; runtime_seconds: int
def authority_from_mapping(repository: str, value: object) -> Authority | None
def check_authority(authority: Authority | None, binding: AuthorityBinding,
                    now: datetime, bound: bool = False) -> str | None
```

Verification:

- Contract: each `check_authority` reason and its order. Mode: focused-test —
  `test_check_authority_reasons` parametrized over missing, revoked (flag,
  removed approval when bound), revision change when bound (approved), mismatch (version, digest, repository),
  expired, scope (action, file), limits (calls, cost, runtime), and approve.
  Red: `ModuleNotFoundError: desloppify.engine.repair_authority`. Green: the
  focused command passes.
- Contract: decoding. Mode: focused-test — `test_authority_decoding` covers
  absent → `None`, `schema: 2` and action `merge` → `UnsupportedAuthority`,
  naive `expires_at`, empty `files`, and a duplicate key → `ValueError`.

Steps: write both tests; run the focused command and see the import error;
implement the module (decode with the `_required_text`-style helpers local
to it, `expires_at` via `datetime.fromisoformat` rejecting naive values,
`cost_cap_usd` via `Decimal(str(value))` positive and finite); rerun green;
commit `feat(repair-authority): decode and check operator approvals`.

## Task 2 — State, disposal, and the source comparison export

Files: `desloppify/engine/repair_cycle.py`, `desloppify/app/commands/repair_queue.py`,
`desloppify/app/cli_support/parser_groups_repair_cycle.py`, tests.

Interfaces: `CycleConfig.authority: object = None` (raw `mapping.get("authority")`);
`CycleState.authority: dict[str, str] | None` with keys `issue_id`, `key`,
`revision` (non-empty strings, else `ValueError` on load), written by
`to_mapping`, archived, and cleared by `begin`;
`CycleState.dispose(attempt_id: str, *, confirm_stopped: bool = False)`;
`repair_queue.source_comparison(args, issue) -> _Recheck` (fields `current`,
`reason`, `keep`).

Verification:

- Contract: active-attempt disposal. Mode: focused-test —
  `test_dispose_refuses_an_active_attempt_without_confirmation`: receipt
  `active` raises `CommandError` mentioning `--confirm-stopped`; with
  `confirm_stopped=True` it disposes; a dispatch at `intent` or `unknown`,
  or with outcome `unknown`, also refuses.
  Red: disposal succeeds. Green: focused command.
- Contract: persisted `authority` round-trips; legacy state without it loads;
  a non-string value raises. Mode: focused-test — extend
  `test_legacy_state_decodes_and_new_fields_validate`.
- Contract: parser flag. Mode: focused-test — extend
  `test_parser_wires_dispose_attempt` to parse `--confirm-stopped`.
- Contract: renamed comparison keeps repair-queue behavior. Mode:
  focused-test — the existing `test_repair_queue.py` suite stays green.

Commit `feat(repair-cycle): bind authority state and guard active disposal`.

## Task 3 — `_authorize` at selection, dispatch, and resume

Files: `desloppify/app/commands/repair_cycle.py`, tests, docs.

Interfaces: `_authorize(args, state, cycle_state, config, now, *, select=False)
-> tuple[AuthorityBinding, dict[str, str]] | str`;
`_dispatch_host(args, state, cycle_state, config, adapter, request)`;
`AdeptCycleClient.select(config, lease, binding: AuthorityBinding)`.

`_authorize` follows the spec's order exactly (unbound, trust, decode, work
item via `_selected_issue(state, repository, authority)`, bound key, source,
`check_authority`); binding files are the revalidated manifest's dependency
paths. `_begin_and_select` authorizes with `select=True` before `begin`.

Verification:

- Contract: every command-level reason parks before `client.select` with
  `selections == 0` and no bound record. Mode: focused-test —
  `test_selection_parks_without_authority` parametrized over no link, a
  closed link, invalid block, unsupported schema, missing approval,
  mismatch, revoked, expired, scope, limits, unreadable source
  (`AnalysisUnknown`), and a failing check. Red: selection proceeds.
- Contract: trust by ownership and writability. Mode: focused-test —
  `test_only_a_foreign_read_only_config_grants_authority` monkeypatches
  `repair_cycle.os.geteuid` and `repair_cycle.os.access` (owned file, writable
  file, parent, ancestor → untrusted; foreign read-only → selected), plus a
  missing file → untrusted.
- Contract: selection prefers an approved published repair, records
  `{issue_id, key, revision}` before `client.select`, and passes the binding.
  Mode: focused-test — `test_selection_binds_authority_before_client_selection`,
  `test_selection_prefers_an_approved_published_repair`.
- Contract: dispatch refuses when unbound, revoked, removed, or expired,
  without admission or lookup. Mode: focused-test —
  `test_dispatch_rechecks_authority`, `test_dispatch_rechecks_source`.
- Contract: resume of an active attempt fails on revocation, a changed brief,
  a different bound key, or no binding. Mode: focused-test —
  `test_resume_rechecks_an_active_attempt` and its siblings.
- Contract: docs. Mode: task-test-not-applicable — operator prose in
  `docs/systemd/repair-cycle.md` has no executable consumer.

Steps: update the shared fixtures first (`_state()` holds a revalidated
concern item with an open `github_repair` link; `_config()` holds a matching
approval; `_args` adds `source` and `check` fakes from `test_repair_queue.py`;
`_leased` binds the attempt). Expect red at every `AuthorityProof` reference
and a `TypeError` at each of the 13 `_dispatch_host` calls until they pass
`CONFIG`; an attempt test with an `active` receipt also needs a binding; write
the new tests; implement; rerun the focused command, then the guardrails;
commit `feat(repair-cycle): check trusted authority at selection, dispatch, and resume`.
