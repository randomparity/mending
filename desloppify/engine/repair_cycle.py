"""Durable, scheduler-owned facts for one bounded external repair cycle."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

DEFAULT_RUNTIME_SECONDS = 90 * 60
DEFAULT_CALL_LIMIT = 100
DEFAULT_OBSERVATION_CALL_LIMIT = 3
DEFAULT_OBSERVATION_MINUTES = 5
DEFAULT_WINDOW_MINUTES = 24 * 60
_DISPATCH_PHASES = frozenset({"intent", "returned", "unknown", "settled"})
_WINDOW_EPOCH = datetime(2000, 1, 1)


def window_start(now: datetime, minutes: int) -> str:
    """Key the window holding ``now``: its host-local wall-clock start (ADR 0016)."""
    elapsed = int((now.replace(tzinfo=None) - _WINDOW_EPOCH).total_seconds() // 60)
    start = _WINDOW_EPOCH + timedelta(minutes=elapsed - elapsed % minutes)
    return start.isoformat(timespec="minutes")


@dataclass(frozen=True)
class CycleConfig:
    """Operator-provided bounds; absent model-work inputs deliberately park."""

    enabled: bool
    repository: str
    runtime_seconds: int
    call_limit: int
    model: str | None
    cost_cap_usd: Decimal | None
    host_executable: str = "claude"
    adept_skills_dir: str | None = None
    adept_skills_version: str | None = None
    observation_call_limit: int = DEFAULT_OBSERVATION_CALL_LIMIT
    observation_seconds: int = DEFAULT_OBSERVATION_MINUTES * 60
    window_minutes: int = DEFAULT_WINDOW_MINUTES
    # Decoded only by the authority check (ADR 0015), so a bad block never blocks observation.
    authority: object = None

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
            host_executable=_optional_text(mapping, "host_executable") or "claude",
            adept_skills_dir=_optional_text(mapping, "adept_skills_dir"),
            adept_skills_version=_optional_text(mapping, "adept_skills_version"),
            observation_call_limit=_positive_int(
                mapping, "observation_call_limit", default=DEFAULT_OBSERVATION_CALL_LIMIT
            ),
            observation_seconds=60
            * _positive_int(mapping, "observation_minutes", default=DEFAULT_OBSERVATION_MINUTES),
            window_minutes=_positive_int(mapping, "window_minutes", default=DEFAULT_WINDOW_MINUTES),
            authority=deepcopy(mapping.get("authority")),
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
    window_start: str
    deadline: datetime
    call_limit: int
    cost_cap_usd: Decimal

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("attempt ID is required")
        try:
            datetime.fromisoformat(self.window_start)
        except ValueError as exc:
            raise ValueError("window start is invalid") from exc
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
            window_start=window_start(now, config.window_minutes),
            deadline=now + timedelta(seconds=config.runtime_seconds),
            call_limit=config.call_limit,
            cost_cap_usd=config.cost_cap_usd,
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> CycleLease:
        """Decode one persisted lease; a legacy ``day_key`` is that day's 00:00 window.

        A legacy ``merge_permit`` is ignored: it authorizes nothing (ADR 0016).
        """
        deadline = mapping.get("deadline")
        cost_cap = _decimal(mapping.get("cost_cap_usd"), "USD cost cap")
        if not isinstance(deadline, str):
            raise ValueError("deadline is required")
        try:
            parsed_deadline = datetime.fromisoformat(deadline)
        except ValueError as exc:
            raise ValueError("deadline is invalid") from exc
        if "window_start" in mapping:
            window = _required_text(mapping, "window_start")
        else:
            window = f"{_parse_day_key(_required_text(mapping, 'day_key')).isoformat()}T00:00"
        return cls(
            attempt_id=_required_text(mapping, "attempt_id"),
            window_start=window,
            deadline=parsed_deadline,
            call_limit=_positive_int(mapping, "call_limit"),
            cost_cap_usd=cost_cap,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-safe local scheduler facts only."""
        return {
            "attempt_id": self.attempt_id,
            "window_start": self.window_start,
            "deadline": self.deadline.isoformat(),
            "call_limit": self.call_limit,
            "cost_cap_usd": str(self.cost_cap_usd),
        }


@dataclass(frozen=True)
class BudgetAdmission:
    """The lease budget reserved for one host dispatch."""

    cost_usd: Decimal
    calls: int


@dataclass(frozen=True)
class DispatchRecord:
    """A host dispatch for the current attempt: its intent, then what it left behind.

    ``phase`` is ``intent`` (persisted before launch), ``returned`` (the adapter
    returned an outcome), ``unknown`` (a lease persisted before dispatch
    records existed, whose dispatch may or may not have happened), or
    ``settled`` (every pull request it left is merged or closed).
    """

    attempt_id: str
    phase: str
    session_id: str | None = None
    repository: str | None = None
    repo_root: str | None = None
    prior_worktrees: tuple[str, ...] = ()
    outcome: str | None = None
    pull_requests: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.attempt_id:
            raise ValueError("dispatch attempt ID is required")
        if self.phase not in _DISPATCH_PHASES:
            raise ValueError(f"dispatch phase {self.phase!r} is invalid")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> DispatchRecord:
        """Decode one previously persisted dispatch record."""
        return cls(
            attempt_id=_required_text(mapping, "attempt_id"),
            phase=_required_text(mapping, "phase"),
            session_id=_optional_text(mapping, "session_id"),
            repository=_optional_text(mapping, "repository"),
            repo_root=_optional_text(mapping, "repo_root"),
            prior_worktrees=_texts(mapping, "prior_worktrees"),
            outcome=_optional_text(mapping, "outcome"),
            pull_requests=_texts(mapping, "pull_requests"),
            issues=_texts(mapping, "issues"),
            worktrees=_texts(mapping, "worktrees"),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return JSON-safe dispatch facts."""
        return {
            "attempt_id": self.attempt_id,
            "phase": self.phase,
            "session_id": self.session_id,
            "repository": self.repository,
            "repo_root": self.repo_root,
            "prior_worktrees": list(self.prior_worktrees),
            "outcome": self.outcome,
            "pull_requests": list(self.pull_requests),
            "issues": list(self.issues),
            "worktrees": list(self.worktrees),
        }

    def with_references(
        self,
        pull_requests: tuple[str, ...],
        issues: tuple[str, ...],
        worktrees: tuple[str, ...],
    ) -> DispatchRecord:
        """Add found references to the recorded ones; a later lookup never removes one."""
        return replace(
            self,
            pull_requests=_union(self.pull_requests, pull_requests),
            issues=_union(self.issues, issues),
            worktrees=_union(self.worktrees, worktrees),
        )


@dataclass
class CycleState:
    """The minimal state needed to reconcile one external scheduler attempt."""

    current_lease: CycleLease | None = None
    authoritative_receipt: dict[str, object] | None = None
    parked_reason: str | None = None
    observation_calls: int = 0
    attempt_failure: str | None = None
    disposed_attempt: str | None = None
    consumed_cost_usd: Decimal = Decimal(0)
    consumed_calls: int = 0
    reserved_cost_usd: Decimal = Decimal(0)
    reserved_calls: int = 0
    dispatch: DispatchRecord | None = None
    authority: dict[str, str] | None = None
    attempt_history: list[dict[str, object]] = field(default_factory=list)

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
        if lease_value is not None and not isinstance(lease_value, Mapping):
            raise ValueError("current lease is invalid")
        if receipt_value is not None and not isinstance(receipt_value, Mapping):
            raise ValueError("authoritative receipt is invalid")
        if parked_reason is not None and not isinstance(parked_reason, str):
            raise ValueError("parked reason is invalid")
        lease = CycleLease.from_mapping(lease_value) if lease_value else None
        return cls(
            current_lease=lease,
            authoritative_receipt=dict(receipt_value) if receipt_value else None,
            parked_reason=parked_reason,
            observation_calls=_count(mapping, "observation_calls"),
            attempt_failure=_optional_text(mapping, "attempt_failure"),
            disposed_attempt=_optional_text(mapping, "disposed_attempt"),
            consumed_cost_usd=_cost(mapping, "consumed_cost_usd"),
            consumed_calls=_count(mapping, "consumed_calls"),
            reserved_cost_usd=_cost(mapping, "reserved_cost_usd"),
            reserved_calls=_count(mapping, "reserved_calls"),
            dispatch=_dispatch(mapping, lease),
            authority=_bound_authority(mapping.get("authority")),
            attempt_history=_history(mapping),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return state suitable for embedding in the existing JSON state file."""
        return {
            "current_lease": self.current_lease.to_mapping() if self.current_lease else None,
            "authoritative_receipt": self.authoritative_receipt,
            "parked_reason": self.parked_reason,
            "observation_calls": self.observation_calls,
            "attempt_failure": self.attempt_failure,
            "disposed_attempt": self.disposed_attempt,
            "consumed_cost_usd": str(self.consumed_cost_usd),
            "consumed_calls": self.consumed_calls,
            "reserved_cost_usd": str(self.reserved_cost_usd),
            "reserved_calls": self.reserved_calls,
            "dispatch": self.dispatch.to_mapping() if self.dispatch else None,
            "authority": self.authority,
            "attempt_history": list(self.attempt_history),
        }

    def begin(self, config: CycleConfig, now: datetime, *, attempt_id: str | None = None) -> CycleLease | None:
        """Persist a new lease only when the attempt is settled and from an earlier window."""
        window = window_start(now, config.window_minutes)
        if self.current_lease is not None and not self._may_replace_lease(window):
            return None
        if self.current_lease is not None:
            self.attempt_history.append(self._archive(self.current_lease))
        self.current_lease = CycleLease.create(config, now, attempt_id=attempt_id)
        self.authoritative_receipt = None
        self.parked_reason = None
        self.observation_calls = 0
        self.attempt_failure = None
        self.disposed_attempt = None
        self.consumed_cost_usd = self.reserved_cost_usd = Decimal(0)
        self.consumed_calls = self.reserved_calls = 0
        self.dispatch = None
        self.authority = None
        return self.current_lease

    def _archive(self, lease: CycleLease) -> dict[str, object]:
        return {
            "lease": lease.to_mapping(),
            "authoritative_receipt": self.authoritative_receipt,
            "parked_reason": self.parked_reason,
            "attempt_failure": self.attempt_failure,
            "disposed": self.disposed,
            "observation_calls": self.observation_calls,
            "consumed_cost_usd": str(self.consumed_cost_usd),
            "consumed_calls": self.consumed_calls,
            "reserved_cost_usd": str(self.reserved_cost_usd),
            "reserved_calls": self.reserved_calls,
            "dispatch": self.dispatch.to_mapping() if self.dispatch else None,
            "authority": self.authority,
        }

    def admit(self) -> BudgetAdmission | None:
        """Reserve the lease's whole remaining budget, or None when nothing remains."""
        lease = self.current_lease
        if lease is None:
            raise ValueError("no current lease")
        cost = lease.cost_cap_usd - self.consumed_cost_usd - self.reserved_cost_usd
        calls = lease.call_limit - self.consumed_calls - self.reserved_calls
        if cost <= 0 or calls <= 0:
            return None
        self.reserved_cost_usd += cost
        self.reserved_calls += calls
        return BudgetAdmission(cost, calls)

    def settle(self, admission: BudgetAdmission, cost_usd: Decimal | None, calls: int) -> None:
        """Release a reservation and charge measured usage; unmeasured cost charges it all."""
        self.reserved_cost_usd -= admission.cost_usd
        self.reserved_calls -= admission.calls
        self.consumed_cost_usd += admission.cost_usd if cost_usd is None else cost_usd
        self.consumed_calls += calls

    @property
    def budget_exceeded(self) -> bool:
        """Return whether consumed usage is over the current lease's ceilings."""
        lease = self.current_lease
        return lease is not None and (
            self.consumed_cost_usd > lease.cost_cap_usd or self.consumed_calls > lease.call_limit
        )

    def record_receipt(self, receipt: Mapping[str, object]) -> None:
        """Persist an already-validated receipt for restart reconciliation."""
        self.authoritative_receipt = dict(receipt)
        self.parked_reason = None

    def park(self, reason: str) -> None:
        """Record a fail-closed state without discarding the active attempt."""
        self.parked_reason = reason

    def fail(self, reason: str) -> None:
        """Retain the attempt's first failure and park; only a disposition releases it."""
        if self.attempt_failure is None:
            self.attempt_failure = reason
        self.park(reason)

    @property
    def awaiting_disposition(self) -> bool:
        """Return whether a failed attempt blocks new work until an operator disposes it."""
        return (
            self.current_lease is not None
            and self.attempt_failure is not None
            and not self.disposed
        )

    @property
    def disposed(self) -> bool:
        """Return whether the operator disposed the current attempt."""
        lease = self.current_lease
        return lease is not None and self.disposed_attempt == lease.attempt_id

    def dispose(self, attempt_id: str, *, confirm_stopped: bool = False) -> None:
        """Record the operator's disposition of the current failed attempt.

        An attempt last reported active needs ``confirm_stopped``: the operator's
        assertion that its worker and pull request are finished.
        """
        lease = self.current_lease
        if lease is None or lease.attempt_id != attempt_id:
            current = lease.attempt_id if lease else "none"
            raise ValueError(
                f"attempt {attempt_id} is not the current attempt (current: {current})"
            )
        if self.attempt_failure is None:
            raise ValueError(f"attempt {attempt_id} has no recorded failure to dispose")
        if self.reported_active and not confirm_stopped:
            raise ValueError(
                f"attempt {attempt_id} was last reported active; confirm on the host that its "
                "worker and pull request are finished, then pass --confirm-stopped"
            )
        self.disposed_attempt = attempt_id

    @property
    def reported_active(self) -> bool:
        """Return whether a worker or an open pull request may still belong to the attempt."""
        receipt = self.authoritative_receipt or {}
        dispatch = self.dispatch
        if receipt.get("state") == "active":
            return True
        if dispatch is None or dispatch.phase == "settled":
            return False
        return (
            dispatch.phase != "returned"
            or dispatch.outcome in {"unknown", "completed"}
            or bool(dispatch.pull_requests)
        )

    @property
    def settled(self) -> bool:
        """Return whether nothing of the current attempt can still be running or open."""
        if self.current_lease is None or self.disposed:
            return True
        receipt = self.authoritative_receipt or {}
        if receipt.get("state") == "terminal":
            return True
        if self.dispatch is not None:
            return self.dispatch.phase == "settled"
        return not (self.reported_active or self.reserved_calls or self.reserved_cost_usd)

    def _may_replace_lease(self, window: str) -> bool:
        lease = self.current_lease
        return (
            lease is not None
            and lease.window_start != window
            and self.settled
            and not self.awaiting_disposition
        )


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


def _texts(mapping: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _union(recorded: tuple[str, ...], found: tuple[str, ...]) -> tuple[str, ...]:
    return recorded + tuple(item for item in dict.fromkeys(found) if item not in recorded)


def _dispatch(mapping: Mapping[str, object], lease: CycleLease | None) -> DispatchRecord | None:
    if "dispatch" not in mapping:
        # State from before dispatch records: whether this lease dispatched is unknown.
        return DispatchRecord(lease.attempt_id, "unknown") if lease else None
    value = mapping["dispatch"]
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("dispatch record is invalid")
    record = DispatchRecord.from_mapping(value)
    if lease is None or record.attempt_id != lease.attempt_id:
        raise ValueError("dispatch record does not match the current lease")
    return record


def _bound_authority(value: object) -> dict[str, str] | None:
    # A malformed record reads as unbound, which every later check refuses (ADR 0015);
    # raising here would block observation and disposal of the attempt instead.
    keys = ("issue_id", "key", "revision")
    if not isinstance(value, Mapping) or set(value) != set(keys) or not all(
        isinstance(value[key], str) and value[key] for key in keys
    ):
        return None
    return {key: value[key] for key in keys}


def _history(mapping: Mapping[str, object]) -> list[dict[str, object]]:
    value = mapping.get("attempt_history", [])
    if not isinstance(value, list):
        raise ValueError("attempt history must be a list")
    entries: list[dict[str, object]] = []
    for entry in value:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("lease"), Mapping):
            raise ValueError("attempt history entry needs a lease")
        CycleLease.from_mapping(entry["lease"])
        dispatch = entry.get("dispatch")
        if dispatch is not None:
            if not isinstance(dispatch, Mapping):
                raise ValueError("attempt history dispatch is invalid")
            DispatchRecord.from_mapping(dispatch)
        entries.append(dict(entry))
    return entries


def _count(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _cost(mapping: Mapping[str, object], key: str) -> Decimal:
    value = mapping.get(key, "0")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{key} must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{key} must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{key} must be non-negative")
    return parsed


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


__all__ = [
    "BudgetAdmission",
    "CycleConfig",
    "CycleLease",
    "CycleState",
    "DispatchRecord",
    "DEFAULT_CALL_LIMIT",
    "DEFAULT_RUNTIME_SECONDS",
]
