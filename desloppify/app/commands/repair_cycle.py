"""One-shot, fail-closed orchestration for an external Adept repair cycle."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar, cast

from desloppify.app.commands.helpers.state import state_path
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import save_state, state_lock
from desloppify.engine._state.schema_types import StateModel
from desloppify.engine.repair_cycle import CycleConfig, CycleLease, CycleState

_Result = TypeVar("_Result")


@dataclass(frozen=True)
class AuthorityProof:
    """Repository-bound policy facts that must precede new model work."""

    repository: str
    policy_id: str
    policy_revision: str
    proof_id: str


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
    """The Adept-owned authority, reconciliation, and selection boundary."""

    def verify_authority(self, repository: str) -> AuthorityProof: ...

    def reconcile(self, repository: str, lease: CycleLease) -> AdeptReceipt: ...

    def select(
        self,
        config: CycleConfig,
        lease: CycleLease,
        authority: AuthorityProof,
    ) -> AdeptReceipt: ...


class _UnavailableAdeptCycleClient:
    """Prevent live dispatch until Adept installs its owned protocol adapter."""

    def verify_authority(self, repository: str) -> AuthorityProof:
        raise RuntimeError("Adept repair-cycle adapter is not installed")

    def reconcile(self, repository: str, lease: CycleLease) -> AdeptReceipt:
        raise RuntimeError("Adept repair-cycle adapter is not installed")

    def select(
        self,
        config: CycleConfig,
        lease: CycleLease,
        authority: AuthorityProof,
    ) -> AdeptReceipt:
        raise RuntimeError("Adept repair-cycle adapter is not installed")


def cmd_repair_cycle(args: argparse.Namespace) -> None:
    """Run at most one local scheduler attempt and park on every unsafe outcome."""
    config = _load_config(args)
    now = _now(args)
    client = cast(AdeptCycleClient, getattr(args, "client", None) or _UnavailableAdeptCycleClient())
    with _locked_state(args) as state:
        cycle_state = _cycle_state(state)
        if cycle_state.current_lease is not None:
            if _has_terminal_receipt(cycle_state):
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
    lease = cycle_state.begin(config, now or _now(args))
    if lease is None:
        _park(state, cycle_state, "daily-attempt-complete")
        return
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    _select(args, state, cycle_state, config, client, lease)


def _select(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
    lease: CycleLease,
) -> None:
    try:
        authority = _call_before_deadline(
            args,
            lease,
            lambda: client.verify_authority(config.repository),
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    except Exception:
        _park(state, cycle_state, "authority-unavailable")
        return
    if not _valid_authority(authority, config.repository):
        _park(state, cycle_state, "invalid-authority-proof")
        return
    try:
        receipt = _call_before_deadline(
            args,
            lease,
            lambda: client.select(config, lease, authority),
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
    try:
        receipt = _call_before_deadline(
            args,
            lease,
            lambda: client.reconcile(config.repository, lease),
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return False
    except Exception:
        _park(state, cycle_state, "reconciliation-unavailable")
        return False
    accepted = _accept_receipt(state, cycle_state, lease, receipt, _now(args))
    return accepted and isinstance(receipt, AdeptReceipt) and receipt.state == "terminal"


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
    reason = _receipt_reason(cycle_state, lease, receipt, now)
    if reason is not None:
        _park(state, cycle_state, reason)
        return False
    if receipt.state == "unknown":
        _park(state, cycle_state, "unknown-receipt")
        return False
    cycle_state.record_receipt(receipt.to_mapping())
    if receipt.merge_consumed:
        cycle_state.consume_merge_permit(lease)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle {receipt.state}.")
    return True


def _receipt_reason(
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: AdeptReceipt,
    now: datetime,
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
    if now > lease.deadline:
        return "runtime-exhausted"
    if receipt.calls > lease.call_limit or receipt.cost_usd > lease.cost_cap_usd:
        return "budget-exhausted"
    if receipt.merge_consumed and not _merge_permit_accepts(cycle_state, lease, receipt):
        return "merge-permit-exhausted"
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
    if threading.current_thread() is not threading.main_thread():
        return operation()
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, remaining_seconds)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _deadline_exceeded(_signum: int, _frame: object) -> None:
    raise TimeoutError("repair-cycle runtime exhausted")


def _valid_authority(proof: object, repository: str) -> bool:
    return (
        isinstance(proof, AuthorityProof)
        and proof.repository == repository
        and all(isinstance(value, str) and value.strip() for value in (
            proof.policy_id,
            proof.policy_revision,
            proof.proof_id,
        ))
    )


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


def _park(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.park(reason)
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


__all__ = ["AdeptCycleClient", "AdeptReceipt", "AuthorityProof", "cmd_repair_cycle"]
