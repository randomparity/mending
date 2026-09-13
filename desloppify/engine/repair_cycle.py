"""Durable, scheduler-owned facts for one bounded external repair cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

DEFAULT_RUNTIME_SECONDS = 90 * 60
DEFAULT_CALL_LIMIT = 100


@dataclass(frozen=True)
class CycleConfig:
    """Operator-provided bounds; absent model-work inputs deliberately park."""

    enabled: bool
    repository: str
    runtime_seconds: int
    call_limit: int
    model: str | None
    cost_cap_usd: Decimal | None

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> CycleConfig:
        """Decode supported configuration without inferring model-work authority."""
        enabled = _bool(mapping, "enabled", default=False)
        repository = _required_text(mapping, "repository")
        runtime_minutes = _positive_int(mapping, "runtime_minutes", default=90)
        call_limit = _positive_int(mapping, "call_limit", default=DEFAULT_CALL_LIMIT)
        return cls(
            enabled=enabled,
            repository=repository,
            runtime_seconds=runtime_minutes * 60,
            call_limit=call_limit,
            model=_optional_text(mapping, "model"),
            cost_cap_usd=_optional_cost_cap(mapping),
        )

    @property
    def park_reason(self) -> str | None:
        """Return why this configuration may not invoke model-backed selection."""
        if not self.enabled:
            return "disabled"
        if self.model is None:
            return "missing-model"
        if self.cost_cap_usd is None:
            return "missing-cost-cap"
        return None


@dataclass(frozen=True)
class CycleLease:
    """A persisted, correlated scheduler attempt created before external I/O."""

    attempt_id: str
    day_key: str
    deadline: datetime
    call_limit: int
    cost_cap_usd: Decimal
    merge_permit: bool

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt ID is required")
        _parse_day_key(self.day_key)
        if self.deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        if self.call_limit < 1:
            raise ValueError("call limit must be positive")
        if not self.cost_cap_usd.is_finite() or self.cost_cap_usd <= 0:
            raise ValueError("USD cost cap must be positive")

    @classmethod
    def create(cls, config: CycleConfig, now: datetime, *, attempt_id: str | None = None) -> CycleLease:
        """Create a lease using the caller's host-local timestamp."""
        if config.cost_cap_usd is None:
            raise ValueError("USD cost cap is required")
        if now.tzinfo is None:
            raise ValueError("host-local time must include a timezone")
        return cls(
            attempt_id=attempt_id or uuid4().hex,
            day_key=now.date().isoformat(),
            deadline=now + timedelta(seconds=config.runtime_seconds),
            call_limit=config.call_limit,
            cost_cap_usd=config.cost_cap_usd,
            merge_permit=True,
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> CycleLease:
        """Decode one previously persisted lease."""
        deadline = mapping.get("deadline")
        cost_cap = _decimal(mapping.get("cost_cap_usd"), "USD cost cap")
        if not isinstance(deadline, str):
            raise ValueError("deadline is required")
        try:
            parsed_deadline = datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise ValueError("deadline is invalid") from exc
        return cls(
            attempt_id=_required_text(mapping, "attempt_id"),
            day_key=_required_text(mapping, "day_key"),
            deadline=parsed_deadline,
            call_limit=_positive_int(mapping, "call_limit"),
            cost_cap_usd=cost_cap,
            merge_permit=_bool(mapping, "merge_permit"),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-safe local scheduler facts only."""
        return {
            "attempt_id": self.attempt_id,
            "day_key": self.day_key,
            "deadline": self.deadline.isoformat(),
            "call_limit": self.call_limit,
            "cost_cap_usd": str(self.cost_cap_usd),
            "merge_permit": self.merge_permit,
        }


@dataclass
class CycleState:
    """The minimal state needed to reconcile one external scheduler attempt."""

    current_lease: CycleLease | None = None
    authoritative_receipt: dict[str, object] | None = None
    parked_reason: str | None = None
    merge_permit_day: str | None = None

    @classmethod
    def empty(cls) -> CycleState:
        """Create state with no recorded attempt."""
        return cls()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object] | None) -> CycleState:
        """Decode scheduler state, rejecting malformed persisted facts."""
        if mapping is None:
            return cls.empty()
        lease_value = mapping.get("current_lease")
        receipt_value = mapping.get("authoritative_receipt")
        parked_reason = mapping.get("parked_reason")
        merge_day = mapping.get("merge_permit_day")
        if lease_value is not None and not isinstance(lease_value, Mapping):
            raise ValueError("current lease is invalid")
        if receipt_value is not None and not isinstance(receipt_value, Mapping):
            raise ValueError("authoritative receipt is invalid")
        if parked_reason is not None and not isinstance(parked_reason, str):
            raise ValueError("parked reason is invalid")
        if merge_day is not None:
            if not isinstance(merge_day, str):
                raise ValueError("merge permit day is invalid")
            _parse_day_key(merge_day)
        return cls(
            current_lease=CycleLease.from_mapping(lease_value) if lease_value else None,
            authoritative_receipt=dict(receipt_value) if receipt_value else None,
            parked_reason=parked_reason,
            merge_permit_day=merge_day,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return state suitable for embedding in the existing JSON state file."""
        return {
            "current_lease": self.current_lease.to_mapping() if self.current_lease else None,
            "authoritative_receipt": self.authoritative_receipt,
            "parked_reason": self.parked_reason,
            "merge_permit_day": self.merge_permit_day,
        }

    def begin(self, config: CycleConfig, now: datetime, *, attempt_id: str | None = None) -> CycleLease | None:
        """Persist a new daily lease only when no active or same-day attempt exists."""
        if self.current_lease is not None and not self._may_replace_lease(now.date()):
            return None
        self.current_lease = CycleLease.create(config, now, attempt_id=attempt_id)
        self.authoritative_receipt = None
        self.parked_reason = None
        return self.current_lease

    def record_receipt(self, receipt: Mapping[str, object]) -> None:
        """Persist an already-validated receipt for restart reconciliation."""
        self.authoritative_receipt = dict(receipt)
        self.parked_reason = None

    def park(self, reason: str) -> None:
        """Record a fail-closed state without discarding the active attempt."""
        self.parked_reason = reason

    def merge_permit_available(self, now: datetime) -> bool:
        """Return whether the host-local day has not consumed its merge permit."""
        return self.merge_permit_day != now.date().isoformat()

    def consume_merge_permit(self, lease: CycleLease) -> None:
        """Consume the lease's local-day permit after a validated merge receipt."""
        if self.current_lease != lease:
            raise ValueError("lease is not current")
        self.merge_permit_day = lease.day_key

    def _may_replace_lease(self, today: date) -> bool:
        if self.current_lease is None or self.current_lease.day_key == today.isoformat():
            return False
        receipt = self.authoritative_receipt or {}
        return receipt.get("state") == "terminal"


def _bool(mapping: Mapping[str, object], key: str, *, default: bool | None = None) -> bool:
    value = mapping.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _positive_int(mapping: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _optional_text(mapping: Mapping[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_cost_cap(mapping: Mapping[str, object]) -> Decimal | None:
    value = mapping.get("cost_cap_usd")
    return None if value is None else _decimal(value, "USD cost cap")


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _parse_day_key(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("day key is invalid") from exc


__all__ = ["CycleConfig", "CycleLease", "CycleState", "DEFAULT_CALL_LIMIT", "DEFAULT_RUNTIME_SECONDS"]
