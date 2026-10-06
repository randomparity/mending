"""Operator approval for one repair, read from trusted configuration (ADR 0015)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

AUTHORITY_SCHEMA = 1
SUPPORTED_ACTIONS = frozenset({"repair"})


class UnsupportedAuthority(ValueError):
    """The authority block uses a schema or action this version does not implement."""


@dataclass(frozen=True)
class Approval:
    """One operator approval of one reviewed brief at one evidence version."""

    key: str
    reviewed_brief_version: str
    evidence_digest: str
    files: frozenset[str]
    actions: frozenset[str]
    call_limit: int
    cost_cap_usd: Decimal
    runtime_minutes: int
    expires_at: datetime
    revoked: bool


@dataclass(frozen=True)
class Authority:
    """The configuration's approvals for its one repository, at one revision."""

    repository: str
    revision: str
    approvals: tuple[Approval, ...]


@dataclass(frozen=True)
class AuthorityBinding:
    """What a repair attempt would do, built from the current work item and limits."""

    repository: str
    key: str
    reviewed_brief_version: str
    evidence_digest: str
    files: frozenset[str]
    action: str
    call_limit: int
    cost_cap_usd: Decimal
    runtime_seconds: int


def authority_from_mapping(repository: str, value: object) -> Authority | None:
    """Decode the configuration's ``authority`` block; ``None`` when it is absent."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("authority must be an object")
    if value.get("schema") != AUTHORITY_SCHEMA:
        raise UnsupportedAuthority("authority schema is not supported")
    approvals = value.get("approvals")
    if not isinstance(approvals, list):
        raise ValueError("authority approvals must be a list")
    decoded = tuple(map(_approval, approvals))
    if len({approval.key for approval in decoded}) != len(decoded):
        raise ValueError("each key may have only one approval")
    return Authority(repository, _text(value, "revision"), decoded)


def check_authority(
    authority: Authority | None,
    binding: AuthorityBinding,
    now: datetime,
    bound: bool = False,
) -> str | None:
    """Return why ``binding`` is not authorized at ``now``, or ``None`` when it is.

    ``bound`` says an earlier check already approved this attempt, so a missing
    approval now means it was withdrawn.
    """
    approval = _approval_for(authority, binding.key)
    if authority is None or approval is None:
        return "authority-revoked" if bound else "authority-missing"
    if (
        authority.repository != binding.repository
        or approval.reviewed_brief_version != binding.reviewed_brief_version
        or approval.evidence_digest != binding.evidence_digest
    ):
        return "authority-mismatch"
    if approval.revoked:
        return "authority-revoked"
    if now >= approval.expires_at:
        return "authority-expired"
    if binding.action not in approval.actions or not binding.files <= approval.files:
        return "authority-scope-exceeded"
    if (
        binding.call_limit > approval.call_limit
        or binding.cost_cap_usd > approval.cost_cap_usd
        or binding.runtime_seconds > approval.runtime_minutes * 60
    ):
        return "authority-limits-exceeded"
    return None


def _approval_for(authority: Authority | None, key: str) -> Approval | None:
    if authority is None:
        return None
    return next((approval for approval in authority.approvals if approval.key == key), None)


def _approval(value: object) -> Approval:
    if not isinstance(value, Mapping):
        raise ValueError("each approval must be an object")
    actions = _texts(value, "actions")
    if not actions <= SUPPORTED_ACTIONS:
        raise UnsupportedAuthority("approval names an unsupported action")
    revoked = value.get("revoked", False)
    if not isinstance(revoked, bool):
        raise ValueError("revoked must be a boolean")
    return Approval(
        key=_text(value, "key"),
        reviewed_brief_version=_text(value, "reviewed_brief_version"),
        evidence_digest=_text(value, "evidence_digest"),
        files=_texts(value, "files"),
        actions=actions,
        call_limit=_positive_int(value, "call_limit"),
        cost_cap_usd=_cost(value),
        runtime_minutes=_positive_int(value, "runtime_minutes"),
        expires_at=_time(value),
        revoked=revoked,
    )


def _text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _texts(mapping: Mapping[str, object], key: str) -> frozenset[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{key} must be a non-empty list of non-empty strings")
    return frozenset(value)


def _positive_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _cost(mapping: Mapping[str, object]) -> Decimal:
    value = mapping.get("cost_cap_usd")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("cost_cap_usd must be a decimal string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("cost_cap_usd must be a decimal string") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("cost_cap_usd must be positive")
    return parsed


def _time(mapping: Mapping[str, object]) -> datetime:
    value = _text(mapping, "expires_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 time") from exc
    if parsed.tzinfo is None:
        raise ValueError("expires_at must include a timezone")
    return parsed


__all__ = [
    "AUTHORITY_SCHEMA",
    "SUPPORTED_ACTIONS",
    "Approval",
    "Authority",
    "AuthorityBinding",
    "UnsupportedAuthority",
    "authority_from_mapping",
    "check_authority",
]
