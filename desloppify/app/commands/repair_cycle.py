"""One-shot, fail-closed orchestration for an external Adept repair cycle."""

from __future__ import annotations

import argparse
import json
import os
import signal
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, cast

from desloppify.app.commands.helpers.state import state_path
from desloppify.app.commands.repair_cycle_host import (
    ClaudeHostAdapter,
    HostLookupError,
    HostOutcome,
    HostRequest,
    host_session_id,
)
from desloppify.app.commands.repair_queue import source_comparison
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import save_state, state_lock
from desloppify.engine._state.schema_types import StateModel
from desloppify.engine.repair_authority import (
    Authority,
    AuthorityBinding,
    UnsupportedAuthority,
    authority_from_mapping,
    check_authority,
)
from desloppify.engine.repair_brief import reviewed_version
from desloppify.engine.repair_cycle import (
    CycleConfig,
    CycleLease,
    CycleState,
    DispatchRecord,
)
from desloppify.engine.repair_manifest import SourceManifest, manifest_from_record
from desloppify.engine.repair_queue import (
    PromotionCandidate,
    RepairRecordError,
    candidate_from_issue,
    matching_record,
)
from desloppify.engine.repair_selection import rank_key

_Result = TypeVar("_Result")


@dataclass(frozen=True)
class AdeptReceipt:
    """The narrow correlated outcome the scheduler may persist and reconcile."""

    attempt_id: object
    state: object
    reference: object
    merge_consumed: object
    calls: object
    cost_usd: object
    currency: object

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-safe receipt fields without any external claim payload."""
        return {
            "attempt_id": self.attempt_id,
            "state": self.state,
            "reference": self.reference,
            "merge_consumed": self.merge_consumed,
            "calls": self.calls,
            "cost_usd": str(self.cost_usd),
            "currency": self.currency,
        }


class AdeptCycleClient(Protocol):
    """The reconciliation and selection boundary; authority stays with Mending (ADR 0015)."""

    def reconcile(self, repository: str, lease: CycleLease) -> AdeptReceipt: ...

    def select(
        self,
        config: CycleConfig,
        lease: CycleLease,
        binding: AuthorityBinding,
    ) -> AdeptReceipt: ...


class _UnavailableAdeptCycleClient:
    """Prevent live dispatch until Adept installs its owned protocol adapter."""

    def reconcile(self, repository: str, lease: CycleLease) -> AdeptReceipt:
        raise RuntimeError("Adept repair-cycle adapter is not installed")

    def select(
        self,
        config: CycleConfig,
        lease: CycleLease,
        binding: AuthorityBinding,
    ) -> AdeptReceipt:
        raise RuntimeError("Adept repair-cycle adapter is not installed")


def cmd_repair_cycle(args: argparse.Namespace) -> None:
    """Run at most one local scheduler attempt and park on every unsafe outcome."""
    config = _load_config(args)
    now = _now(args)
    client = cast(AdeptCycleClient, getattr(args, "client", None) or _UnavailableAdeptCycleClient())
    dispose_attempt = getattr(args, "dispose_attempt", None)
    with _locked_state(args) as state:
        cycle_state = _cycle_state(state)
        if dispose_attempt is not None:
            confirm_stopped = getattr(args, "confirm_stopped", False) is True
            _dispose(state, cycle_state, dispose_attempt, confirm_stopped)
            return
        if cycle_state.current_lease is not None:
            if _has_terminal_receipt(cycle_state) or cycle_state.disposed:
                _begin_and_select(args, state, cycle_state, config, client)
                return
            if _reconcile(args, state, cycle_state, config, client):
                _begin_and_select(args, state, cycle_state, config, client)
            return
        if config.park_reason is not None:
            _park(state, cycle_state, config.park_reason)
            return
        _begin_and_select(args, state, cycle_state, config, client, now)


def _begin_and_select(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
    now: datetime | None = None,
) -> None:
    if config.park_reason is not None:
        return
    if cycle_state.awaiting_disposition:
        _park(state, cycle_state, "disposition-required")
        return
    now = now or _now(args)
    # Authorized before the lease exists, so a refusal leaves no attempt to reconcile.
    try:
        authorized = _call_within(
            config.runtime_seconds,
            lambda: _authorize(args, state, cycle_state, config, now, select=True),
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    if isinstance(authorized, str):
        _park(state, cycle_state, authorized)
        return
    binding, bound = authorized
    lease = cycle_state.begin(config, now)
    if lease is None:
        _park(state, cycle_state, "daily-attempt-complete")
        return
    cycle_state.authority = bound
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    _select(args, state, cycle_state, config, client, lease, binding)


def _select(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
    lease: CycleLease,
    binding: AuthorityBinding,
) -> None:
    try:
        receipt = _call_before_deadline(
            args,
            lease,
            lambda: client.select(config, lease, binding),
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    except Exception:
        _park(state, cycle_state, "selection-unavailable")
        return
    _accept_receipt(state, cycle_state, lease, receipt, _now(args))


def _reconcile(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
) -> bool:
    lease = cycle_state.current_lease
    if lease is None:
        return False
    if cycle_state.observation_calls >= config.observation_call_limit:
        _fail(state, cycle_state, "observation-exhausted")
        return False
    cycle_state.observation_calls += 1
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    try:
        receipt = _call_within(
            config.observation_seconds,
            lambda: client.reconcile(config.repository, lease),
        )
    except TimeoutError:
        _park(state, cycle_state, "observation-timeout")
        return False
    except Exception:
        _park(state, cycle_state, "reconciliation-unavailable")
        return False
    accepted = _accept_receipt(state, cycle_state, lease, receipt, _now(args))
    if accepted and isinstance(receipt, AdeptReceipt) and receipt.state == "active":
        _recheck_active(args, state, cycle_state, config)
        return False
    return accepted and isinstance(receipt, AdeptReceipt) and receipt.state == "terminal"


def _recheck_active(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
) -> None:
    """On resume, an active attempt whose authority no longer holds fails (ADR 0015)."""
    try:
        authorized = _call_within(
            config.observation_seconds,
            lambda: _authorize(args, state, cycle_state, config, _now(args)),
        )
    except TimeoutError:
        _park(state, cycle_state, "observation-timeout")
        return
    if isinstance(authorized, str):
        _fail(state, cycle_state, authorized)


def _authorize(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    now: datetime,
    *,
    select: bool = False,
) -> tuple[AuthorityBinding, dict[str, str]] | str:
    """The one authority check (ADR 0015): the binding and its record, else a park reason.

    Selection (``select``) picks the work item; every later check rebuilds the
    binding from the work item the attempt was bound to.
    """
    bound = None if select else cycle_state.authority
    if not select and bound is None:
        return "authority-missing"
    authority = _trusted_authority(args, config)
    if isinstance(authority, str):
        return authority
    issue_id = bound["issue_id"] if bound else _selected_issue(state, config.repository, authority)
    items = state.get("work_items")
    issue = items.get(issue_id) if isinstance(items, Mapping) and issue_id else None
    if not isinstance(issue, Mapping):
        return "selected-repair-unavailable"
    candidate = candidate_from_issue(issue, config.repository)
    version = reviewed_version(issue, candidate) if candidate else None
    if candidate is None or version is None:
        return "selected-repair-unavailable"
    if bound and bound["key"] != candidate.key:
        return "authority-mismatch"
    recheck = source_comparison(args, issue)
    if not recheck.current:
        return "source-unreadable" if recheck.keep else "source-not-current"
    binding = _binding(issue, candidate, version, config)
    reason = check_authority(authority, binding, now, bound is not None)
    if reason is not None or authority is None:
        return reason or "authority-missing"
    return binding, {"issue_id": issue_id, "key": candidate.key, "revision": authority.revision}


def _trusted_authority(args: argparse.Namespace, config: CycleConfig) -> Authority | None | str:
    """Decode the configuration's authority, or name why it cannot grant any."""
    if not _trusted_config(args):
        return "authority-untrusted"
    try:
        return authority_from_mapping(config.repository, config.authority)
    except UnsupportedAuthority:
        return "authority-unsupported"
    except ValueError:
        return "authority-invalid"


def _binding(
    issue: Mapping[str, Any],
    candidate: PromotionCandidate,
    version: str,
    config: CycleConfig,
) -> AuthorityBinding:
    record = matching_record(issue["detail"], "github_repair_revalidated", candidate) or {}
    manifest = manifest_from_record(record.get("manifest"))
    files = manifest.dependencies if isinstance(manifest, SourceManifest) else ()
    return AuthorityBinding(
        repository=candidate.repository,
        key=candidate.key,
        reviewed_brief_version=version,
        evidence_digest=candidate.evidence_digest,
        files=frozenset(dependency.path for dependency in files),
        action="repair",
        call_limit=config.call_limit,
        cost_cap_usd=config.cost_cap_usd or Decimal(0),
        runtime_seconds=config.runtime_seconds,
    )


def _selected_issue(
    state: Mapping[str, object], repository: str, authority: Authority | None
) -> str | None:
    """The published repair to bind: an approved one first, then ADR 0014's rank order."""
    approved = {approval.key for approval in authority.approvals} if authority else set()
    items = state.get("work_items")
    choices = []
    for issue_id, issue in items.items() if isinstance(items, Mapping) else ():
        candidate = candidate_from_issue(issue, repository) if isinstance(issue, Mapping) else None
        if candidate is None:
            continue
        try:
            link = matching_record(issue["detail"], "github_repair", candidate)
        except RepairRecordError:
            continue
        if link is not None and link.get("state") != "closed":
            choices.append((candidate.key not in approved, rank_key(issue, candidate), issue_id))
    return min(choices)[2] if choices else None


def _trusted_config(args: argparse.Namespace) -> bool:
    """Only a configuration this account neither owns nor can write grants authority.

    The coding host runs as this account (ADR 0010), so an owned file could be
    made writable again; every ancestor directory is checked because a writable
    one lets the file be replaced.
    """
    if isinstance(getattr(args, "config_data", None), Mapping):
        return True
    path = Path(str(getattr(args, "config", ""))).resolve()
    account = os.geteuid()
    for entry in (path, *path.parents):
        try:
            owner = entry.stat().st_uid
        except OSError:
            return False
        if owner == account or os.access(entry, os.W_OK):
            return False
    return True


def _accept_receipt(
    state: dict[str, Any],
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: object,
    now: datetime,
) -> bool:
    if not isinstance(receipt, AdeptReceipt):
        _park(state, cycle_state, "invalid-receipt")
        return False
    reason = _receipt_reason(cycle_state, lease, receipt)
    if reason is not None:
        _park(state, cycle_state, reason)
        return False
    if receipt.state == "unknown":
        _park(state, cycle_state, "unknown-receipt")
        return False
    cycle_state.record_receipt(receipt.to_mapping())
    if receipt.merge_consumed:
        cycle_state.consume_merge_permit(lease)
    print(f"Repair cycle {receipt.state}.")
    failure = _limit_failure(lease, receipt, now)
    if failure is not None:
        _fail(state, cycle_state, failure)
        return False
    _store_cycle_state(state, cycle_state)
    return True


def _dispatch_host(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    adapter: ClaudeHostAdapter,
    request: HostRequest,
) -> HostOutcome | None:
    """Admit, persist, run, and settle one host dispatch under the held state lock.

    An attempt that already has a dispatch record is replayed, never launched
    again. Returns None when nothing was dispatched. #31 composes this into the cycle.
    """
    lease = cycle_state.current_lease
    if lease is None or request.attempt_id != lease.attempt_id or request.deadline > lease.deadline:
        raise ValueError("host request does not match the current lease")
    if cycle_state.dispatch is not None:
        _replay_dispatch(state, cycle_state, adapter, request)
        return None
    if cycle_state.reserved_calls or cycle_state.reserved_cost_usd:
        # An interrupted dispatch may still be spending; that outranks every other refusal.
        _fail(state, cycle_state, "unsettled-reservation")
        return None
    if _now(args) >= lease.deadline:
        _fail(state, cycle_state, "runtime-exhausted")
        return None
    authorized = _authorize(args, state, cycle_state, config, _now(args))
    if isinstance(authorized, str):
        _park(state, cycle_state, authorized)
        return None
    try:
        prior_worktrees = adapter.worktrees(request.repo_root)
    except HostLookupError as exc:
        print(f"Repair cycle lookup failed: {exc}")
        _park(state, cycle_state, "dispatch-lookup-unavailable")
        return None
    admission = cycle_state.admit()
    if admission is None:
        _fail(state, cycle_state, "budget-exhausted")
        return None
    record = DispatchRecord(
        lease.attempt_id,
        "intent",
        host_session_id(lease.attempt_id),
        adapter.repository,
        str(request.repo_root.resolve()),
        prior_worktrees,
    )
    cycle_state.dispatch = record
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    outcome = adapter.run(request, admission)
    if outcome.state == "parked":
        cycle_state.dispatch = None
        cycle_state.settle(admission, Decimal(0), outcome.calls)
        _park(state, cycle_state, outcome.reason or "host-parked")
        return outcome
    cycle_state.dispatch = replace(record, phase="returned", outcome=outcome.state)
    if outcome.state in {"stopped", "unknown"}:
        # Work in flight or still running when Mending stopped it may have spent anything.
        cycle_state.settle(admission, None, max(outcome.calls, admission.calls))
    else:
        cycle_state.settle(admission, outcome.cost_usd, outcome.calls)
    if cycle_state.budget_exceeded:
        _fail(state, cycle_state, "budget-exhausted")
    else:
        _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    _add_references(state, cycle_state, adapter, request)
    return outcome


def _replay_dispatch(
    state: dict[str, Any],
    cycle_state: CycleState,
    adapter: ClaudeHostAdapter,
    request: HostRequest,
) -> None:
    """Observe what a recorded dispatch left behind; never launch it again."""
    alive = adapter.worker_alive(request.attempt_id)
    if alive is not False:
        _park(state, cycle_state, "dispatch-in-flight" if alive else "dispatch-unverified")
        return
    found = _add_references(state, cycle_state, adapter, request)
    record = cycle_state.dispatch
    if cycle_state.reserved_calls or cycle_state.reserved_cost_usd:
        _fail(state, cycle_state, "unsettled-reservation")
    elif record is not None and record.phase == "unknown":
        _fail(state, cycle_state, "dispatch-outcome-unknown")
    elif not found:
        _park(state, cycle_state, "dispatch-lookup-unavailable")
    else:
        _park(state, cycle_state, "already-dispatched")


def _add_references(
    state: dict[str, Any],
    cycle_state: CycleState,
    adapter: ClaudeHostAdapter,
    request: HostRequest,
) -> bool:
    """Add what the adapter finds for the recorded dispatch; False when the lookup failed."""
    record = cycle_state.dispatch
    if record is None:
        return False
    try:
        found = adapter.references(
            record.attempt_id,
            record.repository or adapter.repository,
            Path(record.repo_root) if record.repo_root else request.repo_root,
            record.prior_worktrees,
        )
    except HostLookupError as exc:
        print(f"Repair cycle lookup failed: {exc}")
        return False
    # An unknown record has no pre-launch snapshot, so no worktree can be attributed to it.
    worktrees = () if record.phase == "unknown" else found.worktrees
    cycle_state.dispatch = record.with_references(found.pull_requests, found.issues, worktrees)
    _store_cycle_state(state, cycle_state)
    return True


def _receipt_reason(
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: AdeptReceipt,
) -> str | None:
    if not isinstance(receipt.attempt_id, str) or not receipt.attempt_id:
        return "invalid-receipt"
    if receipt.attempt_id != lease.attempt_id:
        return "receipt-correlation-mismatch"
    if not isinstance(receipt.state, str) or receipt.state not in {"active", "terminal", "unknown"}:
        return "invalid-receipt"
    if receipt.reference is not None and not isinstance(receipt.reference, str):
        return "invalid-receipt"
    if not isinstance(receipt.merge_consumed, bool):
        return "invalid-receipt"
    if not isinstance(receipt.currency, str) or receipt.currency != "USD":
        return "non-usd-receipt"
    if isinstance(receipt.calls, bool) or not isinstance(receipt.calls, int) or receipt.calls < 0:
        return "invalid-receipt"
    if not isinstance(receipt.cost_usd, Decimal) or not receipt.cost_usd.is_finite() or receipt.cost_usd < 0:
        return "invalid-receipt"
    if receipt.merge_consumed and not _merge_permit_accepts(cycle_state, lease, receipt):
        return "merge-permit-exhausted"
    return None


def _limit_failure(lease: CycleLease, receipt: AdeptReceipt, now: datetime) -> str | None:
    # The receipt has no completion time, so observation time stands in for it.
    if now > lease.deadline:
        return "runtime-exhausted"
    if receipt.calls > lease.call_limit or receipt.cost_usd > lease.cost_cap_usd:
        return "budget-exhausted"
    return None


def _merge_permit_accepts(
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: AdeptReceipt,
) -> bool:
    if not lease.merge_permit:
        return False
    if cycle_state.merge_permit_day is None:
        return True
    return (
        cycle_state.merge_permit_day == lease.day_key
        and cycle_state.authoritative_receipt == receipt.to_mapping()
    )


def _has_terminal_receipt(cycle_state: CycleState) -> bool:
    lease = cycle_state.current_lease
    receipt = cycle_state.authoritative_receipt
    return (
        lease is not None
        and receipt is not None
        and receipt.get("attempt_id") == lease.attempt_id
        and receipt.get("state") == "terminal"
    )


def _call_before_deadline(
    args: argparse.Namespace,
    lease: CycleLease,
    operation: Callable[[], _Result],
) -> _Result:
    remaining_seconds = (lease.deadline - _now(args)).total_seconds()
    if remaining_seconds <= 0:
        raise TimeoutError("repair-cycle runtime exhausted")
    return _call_within(remaining_seconds, operation)


def _call_within(seconds: float, operation: Callable[[], _Result]) -> _Result:
    if threading.current_thread() is not threading.main_thread():
        return operation()
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _deadline_exceeded(_signum: int, _frame: object) -> None:
    raise TimeoutError("repair-cycle call bound exceeded")


def _load_config(args: argparse.Namespace) -> CycleConfig:
    supplied = getattr(args, "config_data", None)
    if isinstance(supplied, Mapping):
        raw = supplied
    else:
        path = getattr(args, "config", None)
        if not isinstance(path, str) or not path:
            raise CommandError("repair-cycle requires --config", exit_code=2)
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError("repair-cycle configuration is unreadable", exit_code=2) from exc
    if not isinstance(raw, Mapping):
        raise CommandError("repair-cycle configuration must be an object", exit_code=2)
    try:
        return CycleConfig.from_mapping(raw)
    except ValueError as exc:
        raise CommandError(f"repair-cycle configuration is invalid: {exc}", exit_code=2) from exc


def _now(args: argparse.Namespace) -> datetime:
    clock = getattr(args, "clock", None)
    supplied = clock() if callable(clock) else getattr(args, "now", None)
    now = supplied if isinstance(supplied, datetime) else datetime.now().astimezone()
    if now.tzinfo is None:
        raise CommandError("repair-cycle requires host-local timezone-aware time", exit_code=2)
    return now


def _cycle_state(state: Mapping[str, object]) -> CycleState:
    raw = state.get("repair_cycle")
    if raw is not None and not isinstance(raw, Mapping):
        raise CommandError("repair-cycle state is invalid")
    try:
        return CycleState.from_mapping(raw)
    except ValueError as exc:
        raise CommandError(f"repair-cycle state is invalid: {exc}") from exc


def _dispose(
    state: dict[str, Any],
    cycle_state: CycleState,
    attempt_id: str,
    confirm_stopped: bool,
) -> None:
    try:
        cycle_state.dispose(attempt_id, confirm_stopped=confirm_stopped)
    except ValueError as exc:
        raise CommandError(f"repair-cycle cannot dispose attempt: {exc}", exit_code=2) from exc
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle attempt {attempt_id} disposed.")


def _park(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.park(reason)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle parked: {reason}.")


def _fail(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.fail(reason)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle parked: {reason}.")


def _store_cycle_state(state: dict[str, Any], cycle_state: CycleState) -> None:
    state["repair_cycle"] = cycle_state.to_mapping()


def _persist_before_external_call(args: argparse.Namespace, state: dict[str, Any]) -> None:
    if isinstance(getattr(args, "state_data", None), dict):
        return
    save_state(cast(StateModel, state), state_path(args))


@contextmanager
def _locked_state(args: argparse.Namespace) -> Iterator[dict[str, Any]]:
    supplied = getattr(args, "state_data", None)
    if isinstance(supplied, dict):
        yield supplied
        return
    with state_lock(state_path(args)) as state:
        yield cast(dict[str, Any], state)


__all__ = ["AdeptCycleClient", "AdeptReceipt", "cmd_repair_cycle"]
