from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from desloppify.engine.repair_cycle import CycleConfig, CycleLease, CycleState

REPOSITORY = "owner/repository"
NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)


def _configured(**overrides: object) -> dict[str, object]:
    mapping: dict[str, object] = {
        "enabled": True,
        "repository": REPOSITORY,
        "model": "orchestrator-model",
        "cost_cap_usd": "12.50",
    }
    mapping.update(overrides)
    return mapping


def test_config_uses_only_approved_runtime_and_call_defaults() -> None:
    config = CycleConfig.from_mapping(_configured())

    assert config.runtime_seconds == 90 * 60
    assert config.call_limit == 100
    assert config.cost_cap_usd == Decimal("12.50")
    assert config.park_reason is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_minutes", 0),
        ("runtime_minutes", "soon"),
        ("call_limit", 0),
        ("call_limit", True),
        ("cost_cap_usd", "0"),
        ("cost_cap_usd", "many"),
    ],
)
def test_config_rejects_malformed_limits(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        CycleConfig.from_mapping(_configured(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("model", None, "missing-model"),
        ("cost_cap_usd", None, "missing-cost-cap"),
    ],
)
def test_missing_model_work_configuration_parks(field: str, value: object, reason: str) -> None:
    config = CycleConfig.from_mapping(_configured(**{field: value}))

    assert config.park_reason == reason


def test_lease_rejects_invalid_host_local_day_key() -> None:
    with pytest.raises(ValueError, match="day key"):
        CycleLease(
            attempt_id="attempt",
            day_key="2026-99-13",
            deadline=NOW + timedelta(minutes=90),
            call_limit=100,
            cost_cap_usd=Decimal("1"),
            merge_permit=True,
        )


def test_new_day_restores_merge_permit_without_erasing_active_work() -> None:
    config = CycleConfig.from_mapping(_configured())
    state = CycleState.empty()
    lease = state.begin(config, NOW, attempt_id="active-attempt")
    state.consume_merge_permit(lease)

    next_day = NOW + timedelta(days=1)

    assert state.current_lease == lease
    assert state.merge_permit_available(next_day)
    assert state.begin(config, next_day) is None

