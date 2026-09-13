"""One-shot, fail-closed orchestration for an external Adept repair cycle."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from desloppify.app.commands.helpers.state import state_path
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import save_state, state_lock
from desloppify.engine._state.schema_types import StateModel
from desloppify.engine.repair_cycle import CycleConfig, CycleLease, CycleState


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

    attempt_id: str
    state: str
    reference: str | None
    merge_consumed: bool
    calls: int
    cost_usd: Decimal
    currency: str

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
        if config.park_reason is not None:
            _park(state, cycle_state, config.park_reason)
            return
        if cycle_state.current_lease is not None:
            _reconcile(state, cycle_state, config, client, now)
            return
        lease = cycle_state.begin(config, now)
        if lease is None:
            _park(state, cycle_state, "daily-attempt-complete")
            return
        _store_cycle_state(state, cycle_state)
        _persist_before_external_call(args, state)
        _select(state, cycle_state, config, client, lease, now)


def _select(
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
    lease: CycleLease,
    now: datetime,
) -> None:
    try:
        authority = client.verify_authority(config.repository)
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    except (RuntimeError, ValueError):
        _park(state, cycle_state, "authority-unavailable")
        return
    if not _valid_authority(authority, config.repository):
        _park(state, cycle_state, "invalid-authority-proof")
        return
    try:
        receipt = client.select(config, lease, authority)
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    except (RuntimeError, ValueError):
        _park(state, cycle_state, "selection-unavailable")
        return
    _accept_receipt(state, cycle_state, lease, receipt, now)


def _reconcile(
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    client: AdeptCycleClient,
    now: datetime,
) -> None:
    lease = cycle_state.current_lease
    if lease is None:
        return
    try:
        receipt = client.reconcile(config.repository, lease)
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    except (RuntimeError, ValueError):
        _park(state, cycle_state, "reconciliation-unavailable")
        return
    _accept_receipt(state, cycle_state, lease, receipt, now)


def _accept_receipt(
    state: dict[str, Any],
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: object,
    now: datetime,
) -> None:
    if not isinstance(receipt, AdeptReceipt):
        _park(state, cycle_state, "invalid-receipt")
        return
    reason = _receipt_reason(cycle_state, lease, receipt, now)
    if reason is not None:
        _park(state, cycle_state, reason)
        return
    if receipt.state == "unknown":
        _park(state, cycle_state, "unknown-receipt")
        return
    cycle_state.record_receipt(receipt.to_mapping())
    if receipt.merge_consumed:
        cycle_state.consume_merge_permit(lease)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle {receipt.state}.")


def _receipt_reason(
    cycle_state: CycleState,
    lease: CycleLease,
    receipt: AdeptReceipt,
    now: datetime,
) -> str | None:
    if receipt.attempt_id != lease.attempt_id:
        return "receipt-correlation-mismatch"
    if receipt.state not in {"active", "terminal", "unknown"}:
        return "invalid-receipt"
    if receipt.currency != "USD":
        return "non-usd-receipt"
    if isinstance(receipt.calls, bool) or not isinstance(receipt.calls, int) or receipt.calls < 0:
        return "invalid-receipt"
    if not receipt.cost_usd.is_finite() or receipt.cost_usd < 0:
        return "invalid-receipt"
    if now > lease.deadline:
        return "runtime-exhausted"
    if receipt.calls > lease.call_limit or receipt.cost_usd > lease.cost_cap_usd:
        return "budget-exhausted"
    if receipt.merge_consumed and not _merge_permit_accepts(cycle_state, lease):
        return "merge-permit-exhausted"
    return None


def _merge_permit_accepts(cycle_state: CycleState, lease: CycleLease) -> bool:
    return lease.merge_permit and cycle_state.merge_permit_day in {None, lease.day_key}


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
    supplied = getattr(args, "now", None)
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
