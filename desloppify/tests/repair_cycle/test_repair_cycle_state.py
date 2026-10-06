from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from desloppify.engine.repair_cycle import (
    CycleConfig,
    CycleLease,
    CycleState,
    DispatchRecord,
    window_start,
)

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


def test_lease_rejects_an_invalid_window_start() -> None:
    with pytest.raises(ValueError, match="window start"):
        CycleLease(
            attempt_id="attempt",
            window_start="2026-99-13T00:00",
            deadline=NOW + timedelta(minutes=90),
            call_limit=100,
            cost_cap_usd=Decimal("1"),
        )


@pytest.mark.parametrize(
    ("now", "minutes", "expected"),
    [
        (datetime(2026, 9, 13, 23, 59, tzinfo=UTC), 1440, "2026-09-13T00:00"),
        (datetime(2026, 9, 13, 0, 0, tzinfo=UTC), 1440, "2026-09-13T00:00"),
        (datetime(2026, 9, 13, 23, 59, tzinfo=UTC), 360, "2026-09-13T18:00"),
        (datetime(2026, 9, 13, 5, 59, tzinfo=timezone(timedelta(hours=-4))), 360,
         "2026-09-13T00:00"),
    ],
)
def test_window_start_floors_host_local_wall_time(now, minutes, expected) -> None:
    assert window_start(now, minutes) == expected


def test_config_window_defaults_to_a_day_and_validates() -> None:
    assert CycleConfig.from_mapping(_configured()).window_minutes == 1440
    assert CycleConfig.from_mapping(_configured(window_minutes=360)).window_minutes == 360
    with pytest.raises(ValueError, match="window_minutes"):
        CycleConfig.from_mapping(_configured(window_minutes=0))


def test_legacy_day_keyed_lease_decodes_without_a_merge_permit() -> None:
    legacy = {
        "attempt_id": "legacy",
        "day_key": "2026-09-13",
        "deadline": (NOW + timedelta(minutes=90)).isoformat(),
        "call_limit": 100,
        "cost_cap_usd": "1",
        "merge_permit": True,
    }
    lease = CycleLease.from_mapping(legacy)

    assert lease.window_start == "2026-09-13T00:00"
    assert "day_key" not in lease.to_mapping()
    assert "merge_permit" not in lease.to_mapping()
    with pytest.raises(ValueError, match="day key"):
        CycleLease.from_mapping({**legacy, "day_key": "2026-99-13"})
    state = CycleState.from_mapping(
        {"current_lease": legacy, "merge_permit_day": "2026-09-13", "dispatch": None}
    )
    assert state.current_lease == lease
    assert "merge_permit_day" not in state.to_mapping()


def test_same_window_refuses_and_next_window_replaces_a_settled_attempt() -> None:
    config = CycleConfig.from_mapping(_configured(window_minutes=360))
    state = CycleState.empty()
    lease = state.begin(config, NOW, attempt_id="first")
    assert lease is not None and lease.window_start == "2026-09-13T06:00"

    assert state.settled  # nothing was launched
    assert state.begin(config, NOW + timedelta(hours=2)) is None
    assert state.begin(config, NOW + timedelta(hours=3), attempt_id="second") is not None
    assert [entry["lease"]["attempt_id"] for entry in state.attempt_history] == ["first"]


def _launched(**dispatch: object) -> CycleState:
    state = CycleState.empty()
    state.begin(CycleConfig.from_mapping(_configured()), NOW, attempt_id="a1")
    values: dict = {"phase": "returned", "outcome": "completed"}
    values.update(dispatch)
    state.dispatch = DispatchRecord("a1", **values)
    return state


@pytest.mark.parametrize(
    ("state", "settled", "active"),
    [
        (CycleState.empty(), True, False),
        (_launched(phase="settled", pull_requests=("u",)), True, False),
        (_launched(phase="intent", outcome=None), False, True),
        (_launched(phase="unknown", outcome=None), False, True),
        (_launched(outcome="unknown"), False, True),
        (_launched(), False, True),
        (_launched(outcome="failed"), False, False),
        (_launched(outcome="failed", pull_requests=("u",)), False, True),
    ],
)
def test_settlement_and_reported_activity(state, settled, active) -> None:
    assert (state.settled, state.reported_active) == (settled, active)


def test_legacy_receipts_and_reservations_decide_settlement() -> None:
    config = CycleConfig.from_mapping(_configured())
    state = CycleState.empty()
    state.begin(config, NOW, attempt_id="a1")
    state.authoritative_receipt = {"state": "terminal", "merge_consumed": True}
    assert state.settled
    state.authoritative_receipt = {"state": "active"}
    assert not state.settled and state.reported_active
    state.authoritative_receipt = None
    state.reserved_calls = 1
    assert not state.settled
    state.reserved_calls = 0
    state.dispatch = DispatchRecord("a1", "returned", outcome="failed")
    state.fail("host-failed")
    assert state.begin(config, NOW + timedelta(days=1)) is None
    state.dispose("a1")
    assert state.settled
    assert state.begin(config, NOW + timedelta(days=1)) is not None
