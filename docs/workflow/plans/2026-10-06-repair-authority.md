# Implement trusted repair-cycle authority

Goal: replace the client-supplied authority proof with an operator approval
from the trusted cycle configuration, checked at selection, before dispatch,
and on resume. Architecture: a pure engine module decodes and checks
approvals; `repair-cycle` builds the binding from the current work item,
reuses repair-queue's source comparison, and records the bound revision on
the attempt. Spec: `docs/workflow/specs/2026-10-06-repair-authority-design.md`;
ADR 0015. Stack: Python 3.11+, pytest, mypy, ruff, import-linter.

Expected implementation size: 230–300 changed lines (M) — new engine module
(~110), command wiring (~60), state/dispose (~25), parser and docs (~20),
test fixture rework and new cases (~120 net).

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
                    now: datetime, bound_revision: str | None = None) -> str | None
```

Verification:

- Contract: each `check_authority` reason and its order. Mode: focused-test —
  `test_check_authority_reasons` parametrized over missing, revoked (flag,
  revision change, removed approval), mismatch (version, digest, repository),
  expired, scope (action, file), limits (calls, cost, runtime), and approve.
  Red: `ModuleNotFoundError: desloppify.engine.repair_authority`. Green: the
  focused command passes.
- Contract: decoding. Mode: focused-test — `test_authority_decoding` covers
  absent → `None`, `schema: 2` and action `merge` → `UnsupportedAuthority`,
  naive `expires_at` and empty `files` → `ValueError`.

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
  `confirm_stopped=True` it disposes; a dispatch at `intent` also refuses.
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

Interfaces: `_authorize(args, state, cycle_state, config, now) -> AuthorityBinding | str`;
`_dispatch_host(args, state, cycle_state, config, adapter, request)`;
`AdeptCycleClient.select(config, lease, binding: AuthorityBinding)`.

`_authorize` order: issue id from `cycle_state.authority["issue_id"]` or the
`selected` record in `state["repair_queue_selection"][config.repository]`;
`candidate_from_issue(issue, config.repository)` and
`reviewed_version(issue, candidate)` → else `selected-repair-unavailable`;
bound key differs → `authority-mismatch`; `source_comparison` not current →
`source-unreadable` if `keep` else `source-not-current`; `--config` path or
its parent writable (`os.access(..., os.W_OK)`) without `config_data` →
`authority-untrusted`; `authority_from_mapping` → `authority-unsupported` /
`authority-invalid`; binding files are the revalidated manifest's
dependency paths; return `check_authority(...)` or the binding.

Verification:

- Contract: every command-level reason parks before `client.select` with
  `selections == 0`. Mode: focused-test — `test_selection_parks_without_authority`
  parametrized over no selection record, untrusted config file (mode 0o644 in
  `tmp_path`, skipped as root), invalid block, unsupported schema, missing
  approval, changed source (fake source returning another blob), unreadable
  source (`AnalysisUnknown`). Red: selection proceeds. Green: focused command.
- Contract: a read-only config file (0o444 file, 0o555 directory) is trusted.
  Mode: focused-test — `test_read_only_config_is_trusted`.
- Contract: selection records `{issue_id, key, revision}` before
  `client.select` and passes the binding. Mode: focused-test — rework
  `test_lease_is_persisted_before_authority_verification`.
- Contract: dispatch refuses after revocation or with nothing bound, without
  admission or launch. Mode: focused-test — `test_dispatch_rechecks_authority`
  (revision bumped; `authority` cleared) asserts `adapter.runs == 0` and the
  reason.
- Contract: resume of an active attempt fails on revocation and leaves a
  terminal one alone. Mode: focused-test — `test_resume_fails_revoked_active_attempt`.
- Contract: docs. Mode: task-test-not-applicable — operator prose in
  `docs/systemd/repair-cycle.md` has no executable consumer.

Steps: update the shared fixtures first (`_state()` holds a revalidated,
selected concern item; `_config()` holds a matching approval; `_args` adds
`source` and `check` fakes as in `test_repair_queue.py`) and confirm the
existing suite still fails only where `AuthorityProof` is referenced; write
the new tests; implement; rerun the focused command, then the guardrails;
commit `feat(repair-cycle): check trusted authority at selection, dispatch, and resume`.
