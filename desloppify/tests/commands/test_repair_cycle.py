from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from desloppify.app.commands import repair_cycle
from desloppify.app.commands.repair_cycle import (
    AdeptReceipt,
    AuthorityProof,
    _dispatch_host,
    cmd_repair_cycle,
)
from desloppify.app.commands.repair_cycle_host import (
    HostLookupError,
    HostOutcome,
    HostReferences,
    HostRequest,
    host_session_id,
)
from desloppify.base.exception_sets import CommandError
from desloppify.cli import create_parser
from desloppify.engine.repair_cycle import (
    BudgetAdmission,
    CycleConfig,
    CycleState,
    DispatchRecord,
)

REPOSITORY = "owner/repository"
NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)


class _Client:
    def __init__(self) -> None:
        self.verifications = 0
        self.reconciliations = 0
        self.selections = 0
        self.proof = AuthorityProof(REPOSITORY, "policy", "7", "proof")
        self.receipt: AdeptReceipt | None = None
        self.select_error: Exception | None = None

    def verify_authority(self, repository: str) -> AuthorityProof:
        self.verifications += 1
        assert repository == REPOSITORY
        return self.proof

    def reconcile(self, repository: str, lease) -> AdeptReceipt:
        self.reconciliations += 1
        assert repository == REPOSITORY
        return self._receipt(lease, state="terminal")

    def select(self, config, lease, authority) -> AdeptReceipt:
        self.selections += 1
        assert config.repository == REPOSITORY
        assert authority == self.proof
        if self.select_error is not None:
            raise self.select_error
        return self._receipt(lease, state="terminal")

    def _receipt(self, lease, *, state: str) -> AdeptReceipt:
        return self.receipt or AdeptReceipt(
            attempt_id=lease.attempt_id,
            state=state,
            reference="pr:7",
            merge_consumed=False,
            calls=1,
            cost_usd=Decimal("0.25"),
            currency="USD",
        )


def _config(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "enabled": True,
        "repository": REPOSITORY,
        "model": "orchestrator-model",
        "cost_cap_usd": "2.00",
    }
    result.update(overrides)
    return result


def _args(state_data: dict | None, client: _Client, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "command": "repair-cycle",
        "config": None,
        "config_data": _config(),
        "state": None,
        "state_data": state_data,
        "client": client,
        "now": NOW,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _state() -> dict:
    return {}


def test_parser_wires_one_shot_repair_cycle() -> None:
    parser = create_parser()
    args = parser.parse_args(["repair-cycle", "--config", "/etc/mending/repair-cycle.json"])

    assert args.command == "repair-cycle"
    assert args.config == "/etc/mending/repair-cycle.json"


def test_terminal_attempt_restarts_without_second_selection() -> None:
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client))
    cmd_repair_cycle(_args(state, client))

    assert client.selections == 1
    assert client.reconciliations == 0
    assert state["repair_cycle"]["authoritative_receipt"]["state"] == "terminal"


def test_lease_is_persisted_before_authority_verification(tmp_path) -> None:
    state_path = tmp_path / "state.json"

    class _PersistingClient(_Client):
        def verify_authority(self, repository: str) -> AuthorityProof:
            persisted = json.loads(state_path.read_text())
            assert persisted["repair_cycle"]["current_lease"]["attempt_id"]
            return super().verify_authority(repository)

    cmd_repair_cycle(_args(None, _PersistingClient(), state=str(state_path)))


def test_process_lock_allows_only_one_selection_for_concurrent_invocations(tmp_path) -> None:
    state_path = tmp_path / "state.json"

    class _BlockingClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.selection_started = threading.Event()
            self.release_selection = threading.Event()

        def select(self, config, lease, authority) -> AdeptReceipt:
            self.selection_started.set()
            assert self.release_selection.wait(timeout=2)
            return super().select(config, lease, authority)

    client = _BlockingClient()

    def args() -> argparse.Namespace:
        return _args(None, client, state=str(state_path))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(cmd_repair_cycle, args())
        assert client.selection_started.wait(timeout=2)
        second = executor.submit(cmd_repair_cycle, args())
        client.release_selection.set()
        first.result(timeout=5)
        second.result(timeout=5)

    assert client.selections == 1
    assert client.reconciliations == 0


def test_missing_model_or_cost_cap_parks_without_external_calls() -> None:
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client, config_data=_config(model=None)))

    assert client.verifications == 0
    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == "missing-model"


def test_invalid_authority_proof_parks_before_selection() -> None:
    state = _state()
    client = _Client()
    client.proof = AuthorityProof("other/repository", "policy", "7", "proof")

    cmd_repair_cycle(_args(state, client))

    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == "invalid-authority-proof"


def test_malformed_adapter_results_park_before_attribute_access() -> None:
    class _MalformedAuthorityClient(_Client):
        def verify_authority(self, repository: str):
            self.verifications += 1
            return "not-a-proof"

    malformed_authority_state = _state()
    malformed_authority_client = _MalformedAuthorityClient()
    cmd_repair_cycle(_args(malformed_authority_state, malformed_authority_client))

    assert malformed_authority_client.selections == 0
    assert malformed_authority_state["repair_cycle"]["parked_reason"] == "invalid-authority-proof"

    class _MalformedReceiptClient(_Client):
        def select(self, config, lease, authority):
            self.selections += 1
            return "not-a-receipt"

    malformed_receipt_state = _state()
    cmd_repair_cycle(_args(malformed_receipt_state, _MalformedReceiptClient()))

    assert malformed_receipt_state["repair_cycle"]["parked_reason"] == "invalid-receipt"


def test_malformed_receipt_fields_and_transport_failures_park() -> None:
    class _MalformedFieldClient(_Client):
        def select(self, config, lease, authority):
            return AdeptReceipt(
                attempt_id=lease.attempt_id,
                state="terminal",
                reference="pr:7",
                merge_consumed=False,
                calls=1,
                cost_usd="bad",  # type: ignore[arg-type]
                currency="USD",
            )

    malformed_state = _state()
    cmd_repair_cycle(_args(malformed_state, _MalformedFieldClient()))
    assert malformed_state["repair_cycle"]["parked_reason"] == "invalid-receipt"

    class _OfflineClient(_Client):
        def select(self, config, lease, authority):
            raise ConnectionError("adapter offline")

    offline_state = _state()
    cmd_repair_cycle(_args(offline_state, _OfflineClient()))
    assert offline_state["repair_cycle"]["parked_reason"] == "selection-unavailable"


def test_overdue_adapter_result_is_recorded_as_runtime_failure() -> None:
    class _OverdueClient(_Client):
        def select(self, config, lease, authority) -> AdeptReceipt:
            return self._receipt(lease, state="terminal")

    times = iter((NOW, NOW, NOW, NOW + timedelta(minutes=2)))
    state = _state()
    cmd_repair_cycle(
        _args(
            state,
            _OverdueClient(),
            config_data=_config(runtime_minutes=1),
            clock=lambda: next(times),
        )
    )

    assert state["repair_cycle"]["parked_reason"] == "runtime-exhausted"
    assert state["repair_cycle"]["attempt_failure"] == "runtime-exhausted"
    assert state["repair_cycle"]["authoritative_receipt"]["state"] == "terminal"


def test_unknown_reconciliation_parks_without_new_selection() -> None:
    config = CycleConfig.from_mapping(_config())
    state = _state()
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(config, NOW, attempt_id="recorded-attempt")
    assert lease is not None
    state["repair_cycle"] = cycle_state.to_mapping()
    client = _Client()
    client.receipt = AdeptReceipt(
        attempt_id=lease.attempt_id,
        state="unknown",
        reference=None,
        merge_consumed=False,
        calls=0,
        cost_usd=Decimal("0"),
        currency="USD",
    )

    cmd_repair_cycle(_args(state, client))

    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == "unknown-receipt"


def test_execution_timeout_parks_and_budget_overrun_records_usage() -> None:
    timed_out_state = _state()
    timed_out_client = _Client()
    timed_out_client.select_error = TimeoutError()

    cmd_repair_cycle(_args(timed_out_state, timed_out_client))

    assert timed_out_state["repair_cycle"]["parked_reason"] == "timeout"
    assert timed_out_state["repair_cycle"]["authoritative_receipt"] is None
    assert timed_out_state["repair_cycle"]["attempt_failure"] is None

    class _ExhaustedClient(_Client):
        def _receipt(self, lease, *, state: str) -> AdeptReceipt:
            return AdeptReceipt(
                attempt_id=lease.attempt_id,
                state=state,
                reference="pr:7",
                merge_consumed=False,
                calls=101,
                cost_usd=Decimal("0.25"),
                currency="USD",
            )

    exhausted_state = _state()
    exhausted_client = _ExhaustedClient()

    cmd_repair_cycle(_args(exhausted_state, exhausted_client))

    assert exhausted_state["repair_cycle"]["parked_reason"] == "budget-exhausted"
    assert exhausted_state["repair_cycle"]["attempt_failure"] == "budget-exhausted"
    assert exhausted_state["repair_cycle"]["authoritative_receipt"]["calls"] == 101


def test_non_usd_receipt_parks_and_consumed_merge_stays_daily() -> None:
    class _NonUsdClient(_Client):
        def _receipt(self, lease, *, state: str) -> AdeptReceipt:
            return AdeptReceipt(
                attempt_id=lease.attempt_id,
                state=state,
                reference="pr:7",
                merge_consumed=True,
                calls=1,
                cost_usd=Decimal("0.25"),
                currency="EUR",
            )

    state = _state()
    client = _NonUsdClient()

    cmd_repair_cycle(_args(state, client))

    assert state["repair_cycle"]["parked_reason"] == "non-usd-receipt"


def test_next_day_replaces_a_recorded_terminal_lease() -> None:
    config = CycleConfig.from_mapping(_config())
    state = _state()
    cycle_state = CycleState.empty()
    old_lease = cycle_state.begin(config, NOW, attempt_id="old-attempt")
    assert old_lease is not None
    cycle_state.record_receipt(
        AdeptReceipt(
            attempt_id=old_lease.attempt_id,
            state="terminal",
            reference="pr:7",
            merge_consumed=False,
            calls=1,
            cost_usd=Decimal("0.25"),
            currency="USD",
        ).to_mapping()
    )
    state["repair_cycle"] = cycle_state.to_mapping()
    client = _Client()

    cmd_repair_cycle(_args(state, client, now=NOW + timedelta(days=1)))

    assert client.reconciliations == 0
    assert client.selections == 1
    assert state["repair_cycle"]["current_lease"]["day_key"] == "2026-09-14"


def test_disabled_configuration_reconciles_an_existing_lease() -> None:
    config = CycleConfig.from_mapping(_config())
    state = _state()
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(config, NOW, attempt_id="recorded-attempt")
    assert lease is not None
    state["repair_cycle"] = cycle_state.to_mapping()
    client = _Client()

    cmd_repair_cycle(_args(state, client, config_data=_config(enabled=False)))

    assert client.reconciliations == 1
    assert client.selections == 0
    assert state["repair_cycle"]["authoritative_receipt"]["state"] == "terminal"


def test_only_an_exact_replayed_merge_receipt_reuses_a_daily_permit() -> None:
    state = _state()
    config = CycleConfig.from_mapping(_config())
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(config, NOW, attempt_id="recorded-attempt")
    assert lease is not None
    initial_receipt = AdeptReceipt(
        attempt_id=lease.attempt_id,
        state="active",
        reference="pr:7",
        merge_consumed=True,
        calls=1,
        cost_usd=Decimal("0.25"),
        currency="USD",
    )
    cycle_state.record_receipt(initial_receipt.to_mapping())
    cycle_state.consume_merge_permit(lease)
    state["repair_cycle"] = cycle_state.to_mapping()

    client = _Client()
    client.receipt = initial_receipt
    cmd_repair_cycle(_args(state, client))

    assert state["repair_cycle"]["parked_reason"] is None

    client.receipt = AdeptReceipt(
        attempt_id=lease.attempt_id,
        state="active",
        reference="pr:8",
        merge_consumed=True,
        calls=1,
        cost_usd=Decimal("0.25"),
        currency="USD",
    )
    cmd_repair_cycle(_args(state, client))

    assert state["repair_cycle"]["parked_reason"] == "merge-permit-exhausted"


def _recorded_attempt(state: dict, **config: object):
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(
        CycleConfig.from_mapping(_config(**config)), NOW, attempt_id="recorded-attempt"
    )
    assert lease is not None
    state["repair_cycle"] = cycle_state.to_mapping()
    return lease


def test_expired_lease_is_observed_without_execution() -> None:
    state = _state()
    _recorded_attempt(state, runtime_minutes=1)
    client = _Client()

    cmd_repair_cycle(_args(state, client, now=NOW + timedelta(minutes=2)))

    assert client.reconciliations == 1
    assert client.verifications == 0
    assert client.selections == 0
    recorded = state["repair_cycle"]
    assert recorded["authoritative_receipt"]["state"] == "terminal"
    assert recorded["attempt_failure"] == "runtime-exhausted"
    assert recorded["parked_reason"] == "runtime-exhausted"
    assert recorded["observation_calls"] == 1


def test_observation_allowance_is_bounded_and_persisted(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state = _state()
    _recorded_attempt(state, runtime_minutes=1)
    state_path.write_text(json.dumps(state))

    class _OfflineReconcileClient(_Client):
        def reconcile(self, repository: str, lease) -> AdeptReceipt:
            self.reconciliations += 1
            persisted = json.loads(state_path.read_text())
            assert persisted["repair_cycle"]["observation_calls"] == self.reconciliations
            raise ConnectionError("adapter offline")

    client = _OfflineReconcileClient()

    def run() -> None:
        cmd_repair_cycle(
            _args(
                None,
                client,
                state=str(state_path),
                config_data=_config(runtime_minutes=1, observation_call_limit=1),
                now=NOW + timedelta(days=1),
            )
        )

    run()
    first = json.loads(state_path.read_text())["repair_cycle"]
    assert first["parked_reason"] == "reconciliation-unavailable"
    assert first["attempt_failure"] is None

    run()
    second = json.loads(state_path.read_text())["repair_cycle"]
    assert client.reconciliations == 1
    assert client.selections == 0
    assert second["parked_reason"] == "observation-exhausted"
    assert second["attempt_failure"] == "observation-exhausted"
    assert second["current_lease"]["attempt_id"] == "recorded-attempt"


def test_observation_call_is_time_bounded(monkeypatch) -> None:
    bounds: list[float] = []

    def expire_immediately(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            bounds.append(seconds)
            repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_immediately)
    state = _state()
    _recorded_attempt(state, runtime_minutes=1)
    client = _Client()

    cmd_repair_cycle(
        _args(
            state,
            client,
            config_data=_config(runtime_minutes=1, observation_minutes=2),
            now=NOW + timedelta(minutes=2),
        )
    )

    assert bounds == [120]
    assert client.reconciliations == 0
    assert state["repair_cycle"]["parked_reason"] == "observation-timeout"
    assert state["repair_cycle"]["observation_calls"] == 1


def test_failed_attempt_requires_disposition_before_new_work() -> None:
    state = _state()
    _recorded_attempt(state, runtime_minutes=1)
    client = _Client()
    cmd_repair_cycle(_args(state, client, now=NOW + timedelta(minutes=2)))
    assert state["repair_cycle"]["attempt_failure"] == "runtime-exhausted"

    next_day = NOW + timedelta(days=1)
    cmd_repair_cycle(_args(state, client, now=next_day))

    assert (client.reconciliations, client.verifications, client.selections) == (1, 0, 0)
    assert state["repair_cycle"]["parked_reason"] == "disposition-required"
    assert state["repair_cycle"]["current_lease"]["attempt_id"] == "recorded-attempt"

    with pytest.raises(CommandError, match="not the current attempt"):
        cmd_repair_cycle(_args(state, client, now=next_day, dispose_attempt="other"))
    cmd_repair_cycle(_args(state, client, now=next_day, dispose_attempt="recorded-attempt"))
    assert state["repair_cycle"]["disposed_attempt"] == "recorded-attempt"
    assert client.selections == 0

    cmd_repair_cycle(_args(state, client, now=next_day))

    assert client.selections == 1
    recorded = state["repair_cycle"]
    assert recorded["current_lease"]["attempt_id"] != "recorded-attempt"
    assert recorded["attempt_failure"] is None
    assert recorded["disposed_attempt"] is None


def test_disposition_requires_a_recorded_failure() -> None:
    state = _state()
    _recorded_attempt(state)

    with pytest.raises(CommandError, match="no recorded failure"):
        cmd_repair_cycle(_args(state, _Client(), dispose_attempt="recorded-attempt"))
    assert state["repair_cycle"]["disposed_attempt"] is None


def test_parser_wires_dispose_attempt() -> None:
    args = create_parser().parse_args(
        ["repair-cycle", "--config", "/etc/mending/repair-cycle.json", "--dispose-attempt", "a1"]
    )

    assert args.dispose_attempt == "a1"


def test_deadline_timer_interrupts_before_adapter_selection(monkeypatch) -> None:
    def expire_immediately(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_immediately)
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client))

    assert client.verifications == 0
    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == "timeout"


def test_config_host_keys_default_and_decode() -> None:
    defaults = CycleConfig.from_mapping(_config())
    assert (defaults.host_executable, defaults.adept_skills_dir, defaults.adept_skills_version) == (
        "claude",
        None,
        None,
    )
    configured = CycleConfig.from_mapping(
        _config(
            host_executable=" /usr/local/bin/claude ",
            adept_skills_dir="/opt/adept",
            adept_skills_version="7.2.0",
        )
    )
    assert configured.host_executable == "/usr/local/bin/claude"
    assert configured.adept_skills_dir == "/opt/adept"
    assert configured.adept_skills_version == "7.2.0"
    with pytest.raises(ValueError, match="host_executable"):
        CycleConfig.from_mapping(_config(host_executable=""))


def test_config_observation_keys_default_and_decode() -> None:
    defaults = CycleConfig.from_mapping(_config())
    assert (defaults.observation_call_limit, defaults.observation_seconds) == (3, 300)
    configured = CycleConfig.from_mapping(_config(observation_call_limit=5, observation_minutes=2))
    assert (configured.observation_call_limit, configured.observation_seconds) == (5, 120)
    with pytest.raises(ValueError, match="observation_call_limit"):
        CycleConfig.from_mapping(_config(observation_call_limit=0))
    with pytest.raises(ValueError, match="observation_minutes"):
        CycleConfig.from_mapping(_config(observation_minutes=True))


def test_legacy_state_decodes_and_new_fields_validate() -> None:
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(CycleConfig.from_mapping(_config()), NOW, attempt_id="legacy")
    assert lease is not None
    legacy = {
        key: value
        for key, value in cycle_state.to_mapping().items()
        if key
        not in {
            "observation_calls",
            "attempt_failure",
            "disposed_attempt",
            "consumed_cost_usd",
            "consumed_calls",
            "reserved_cost_usd",
            "reserved_calls",
        }
    }

    decoded = CycleState.from_mapping(legacy)

    assert decoded.current_lease == lease
    assert (decoded.observation_calls, decoded.attempt_failure, decoded.disposed_attempt) == (
        0,
        None,
        None,
    )
    assert (decoded.consumed_cost_usd, decoded.consumed_calls) == (Decimal(0), 0)
    assert (decoded.reserved_cost_usd, decoded.reserved_calls) == (Decimal(0), 0)
    for field, bad in (
        ("observation_calls", -1),
        ("consumed_cost_usd", "-0.01"),
        ("consumed_cost_usd", True),
        ("reserved_cost_usd", "NaN"),
        ("reserved_cost_usd", "lots"),
        ("consumed_calls", -1),
        ("reserved_calls", True),
        ("reserved_calls", "2"),
        ("observation_calls", True),
        ("attempt_failure", 3),
        ("disposed_attempt", ""),
    ):
        with pytest.raises(ValueError):
            CycleState.from_mapping({**legacy, field: bad})


def test_dispatch_record_round_trips_and_legacy_lease_is_unknown() -> None:
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(CycleConfig.from_mapping(_config()), NOW, attempt_id="a1")
    assert lease is not None
    cycle_state.dispatch = DispatchRecord(
        "a1", "returned", "s", "o/r", "/repo", ("/repo",), "completed",
        ("https://x/pull/1",), (), ("/w",),
    )
    assert CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping()))) == cycle_state
    record = DispatchRecord("a1", "returned", pull_requests=("p2", "p0"))
    assert record.with_references(("p1", "p2"), ("i1",), ()).pull_requests == ("p2", "p0", "p1")
    assert record.with_references(("p1", "p2"), ("i1",), ()).issues == ("i1",)

    legacy = {key: value for key, value in cycle_state.to_mapping().items()
              if key not in {"dispatch", "attempt_history"}}
    decoded = CycleState.from_mapping(legacy)
    assert decoded.current_lease == lease
    assert decoded.dispatch == DispatchRecord("a1", "unknown")
    assert decoded.attempt_history == []
    assert CycleState.from_mapping({}).dispatch is None

    valid = cycle_state.to_mapping()
    dispatch = valid["dispatch"]
    assert isinstance(dispatch, dict)
    for bad in (
        {**valid, "current_lease": None},
        {**valid, "dispatch": {**dispatch, "attempt_id": "other"}},
        {**valid, "dispatch": {**dispatch, "phase": "other"}},
        {**valid, "dispatch": {**dispatch, "pull_requests": "p1"}},
        {**valid, "dispatch": {**dispatch, "worktrees": [""]}},
        {**valid, "dispatch": "a1"},
        {**valid, "attempt_history": {}},
        {**valid, "attempt_history": [{"lease": None}]},
        {**valid, "attempt_history": [{"lease": lease.to_mapping(), "dispatch": {"phase": "x"}}]},
    ):
        with pytest.raises(ValueError):
            CycleState.from_mapping(bad)


def test_begin_archives_replaced_attempt() -> None:
    config = CycleConfig.from_mapping(_config())
    cycle_state = CycleState.empty()
    lease = cycle_state.begin(config, NOW, attempt_id="a1")
    assert lease is not None
    cycle_state.dispatch = DispatchRecord("a1", "returned", "s", outcome="failed")
    cycle_state.consumed_calls = 4
    cycle_state.fail("host-error")
    cycle_state.dispose("a1")

    assert cycle_state.begin(config, NOW + timedelta(days=1), attempt_id="a2") is not None
    assert cycle_state.begin(config, NOW + timedelta(days=1), attempt_id="a3") is None

    assert cycle_state.dispatch is None
    assert cycle_state.attempt_history == [
        {
            "lease": lease.to_mapping(),
            "authoritative_receipt": None,
            "parked_reason": "host-error",
            "attempt_failure": "host-error",
            "disposed": True,
            "observation_calls": 0,
            "consumed_cost_usd": "0",
            "consumed_calls": 4,
            "reserved_cost_usd": "0",
            "reserved_calls": 0,
            "dispatch": DispatchRecord("a1", "returned", "s", outcome="failed").to_mapping(),
        }
    ]
    round_trip = CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping())))
    assert round_trip.attempt_history == cycle_state.attempt_history


def _budget_state() -> CycleState:
    cycle_state = CycleState.empty()
    cycle_state.begin(CycleConfig.from_mapping(_config(call_limit=10)), NOW, attempt_id="a1")
    return cycle_state


def test_budget_admission_reserves_remainder_and_settles() -> None:
    cycle_state = _budget_state()

    admission = cycle_state.admit()

    assert admission == BudgetAdmission(Decimal("2.00"), 10)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal("2.00"), 10)
    assert cycle_state.admit() is None
    cycle_state.settle(admission, Decimal("0.75"), 3)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal(0), 0)
    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal("0.75"), 3)
    assert not cycle_state.budget_exceeded
    assert cycle_state.admit() == BudgetAdmission(Decimal("1.25"), 7)
    round_trip = CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping())))
    assert round_trip == cycle_state
    cycle_state.fail("budget-exhausted")
    cycle_state.dispose("a1")
    assert cycle_state.begin(CycleConfig.from_mapping(_config()), NOW + timedelta(days=1))
    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal(0), 0)
    assert (cycle_state.reserved_cost_usd, cycle_state.reserved_calls) == (Decimal(0), 0)
    with pytest.raises(ValueError, match="no current lease"):
        CycleState.empty().admit()


def test_unmeasured_cost_charges_whole_admission() -> None:
    cycle_state = _budget_state()
    admission = cycle_state.admit()
    assert admission is not None

    cycle_state.settle(admission, None, 11)

    assert (cycle_state.consumed_cost_usd, cycle_state.consumed_calls) == (Decimal("2.00"), 11)
    assert cycle_state.budget_exceeded
    assert cycle_state.admit() is None


class _FakeHost:
    repository = REPOSITORY

    def __init__(self, outcome: HostOutcome | None = None, *, error: BaseException | None = None,
                 on_run=None, alive: bool | None = False,
                 found: HostReferences | HostLookupError | None = None,
                 snapshot: tuple[str, ...] | HostLookupError = ("/repo",),
                 on_references=None) -> None:
        self.outcome = outcome or HostOutcome("completed", cost_usd=Decimal("0.5"), calls=3)
        self.error = error
        self.on_run = on_run
        self.alive = alive
        self.found = found or HostReferences(("https://example.invalid/pull/1",))
        self.snapshot = snapshot
        self.on_references = on_references
        self.admissions: list[BudgetAdmission] = []
        self.lookups: list[tuple[str, str, Path, tuple[str, ...]]] = []

    def worker_alive(self, attempt_id: str) -> bool | None:
        return self.alive

    def worktrees(self, repo_root: Path) -> tuple[str, ...]:
        if isinstance(self.snapshot, HostLookupError):
            raise self.snapshot
        return self.snapshot

    def references(self, attempt_id, repository, repo_root, prior_worktrees) -> HostReferences:
        self.lookups.append((attempt_id, repository, repo_root, prior_worktrees))
        if self.on_references is not None:
            self.on_references()
        if isinstance(self.found, HostLookupError):
            raise self.found
        return self.found

    def run(self, request: HostRequest, admission: BudgetAdmission) -> HostOutcome:
        self.admissions.append(admission)
        if self.on_run is not None:
            self.on_run()
        if self.error is not None:
            raise self.error
        return self.outcome


def _leased(state: dict) -> tuple[CycleState, HostRequest]:
    cycle_state = _budget_state()
    lease = cycle_state.current_lease
    assert lease is not None
    state["repair_cycle"] = cycle_state.to_mapping()
    request = HostRequest(
        attempt_id=lease.attempt_id,
        brief="Fix the flaky parser.",
        source_revision="0" * 40,
        authorized_scope="desloppify/parser.py",
        repo_root=Path("."),
        deadline=lease.deadline,
    )
    return cycle_state, request


@pytest.mark.parametrize(
    ("outcome", "consumed", "failure", "parked"),
    [
        (HostOutcome("completed", cost_usd=Decimal("0.5"), calls=3), ("0.5", 3), None, None),
        (
            HostOutcome("failed", "host-error", cost_usd=Decimal("2.5"), calls=4),
            ("2.5", 4),
            "budget-exhausted",
            "budget-exhausted",
        ),
        (HostOutcome("parked", "unenforceable-limit"), ("0", 0), None, "unenforceable-limit"),
        (
            HostOutcome("stopped", "call-limit", calls=11),
            ("2.00", 11),
            "budget-exhausted",
            "budget-exhausted",
        ),
        (HostOutcome("unknown", "timeout-survivors", calls=2), ("2.00", 10), None, None),
    ],
)
def test_dispatch_budget_outcomes(outcome, consumed, failure, parked) -> None:
    state = _state()
    cycle_state, request = _leased(state)
    host = _FakeHost(outcome)

    assert _dispatch_host(_args(state, _Client()), state, cycle_state, host, request) == outcome

    assert host.admissions == [BudgetAdmission(Decimal("2.00"), 10)]
    recorded = state["repair_cycle"]
    assert (Decimal(recorded["consumed_cost_usd"]), recorded["consumed_calls"]) == (
        Decimal(consumed[0]),
        consumed[1],
    )
    assert (Decimal(recorded["reserved_cost_usd"]), recorded["reserved_calls"]) == (0, 0)
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (failure, parked)


def test_dispatch_refuses_without_admission() -> None:
    state = _state()
    cycle_state, request = _leased(state)
    host = _FakeHost()
    late = _args(state, _Client(), now=request.deadline)
    assert _dispatch_host(late, state, cycle_state, host, request) is None
    assert state["repair_cycle"]["attempt_failure"] == "runtime-exhausted"
    assert cycle_state.reserved_calls == 0

    state = _state()
    cycle_state, request = _leased(state)
    cycle_state.consumed_calls = 10
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, host, request) is None
    assert state["repair_cycle"]["attempt_failure"] == "budget-exhausted"
    assert host.admissions == []

    for mismatched in (
        replace(request, attempt_id="other"),
        replace(request, deadline=request.deadline + timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError, match="current lease"):
            _dispatch_host(_args(state, _Client()), state, cycle_state, host, mismatched)


def test_dispatch_persists_reservation_before_launch(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    args = _args(None, _Client(), state=str(state_path))
    seen: list[dict] = []

    def read_disk() -> None:
        seen.append(json.loads(state_path.read_text())["repair_cycle"])

    with repair_cycle._locked_state(args) as state:
        cycle_state, request = _leased(state)
        _dispatch_host(args, state, cycle_state, _FakeHost(on_run=read_disk), request)

    assert (seen[0]["reserved_cost_usd"], seen[0]["reserved_calls"]) == ("2.00", 10)
    assert seen[0]["dispatch"] == DispatchRecord(
        "a1", "intent", host_session_id("a1"), REPOSITORY, ".", ("/repo",)
    ).to_mapping()
    settled = json.loads(state_path.read_text())["repair_cycle"]
    assert (settled["reserved_calls"], settled["consumed_calls"]) == (0, 3)
    assert settled["consumed_cost_usd"] == "0.5"


def test_interrupted_dispatch_keeps_reservation(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    args = _args(None, _Client(), state=str(state_path))

    with pytest.raises(KeyboardInterrupt), repair_cycle._locked_state(args) as state:
        cycle_state, request = _leased(state)
        _dispatch_host(args, state, cycle_state, _FakeHost(error=KeyboardInterrupt()), request)

    host = _FakeHost()
    after_deadline = _args(None, _Client(), state=str(state_path), now=request.deadline)
    with repair_cycle._locked_state(after_deadline) as state:
        restarted = repair_cycle._cycle_state(state)
        assert (restarted.reserved_cost_usd, restarted.reserved_calls) == (Decimal("2.00"), 10)
        assert _dispatch_host(after_deadline, state, restarted, host, request) is None

    assert host.admissions == []
    recorded = json.loads(state_path.read_text())["repair_cycle"]
    assert recorded["attempt_failure"] == "unsettled-reservation"
    assert recorded["reserved_calls"] == 10


def test_returned_dispatch_is_persisted_before_reference_lookup(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    args = _args(None, _Client(), state=str(state_path))
    seen: list[dict] = []
    host = _FakeHost(
        on_references=lambda: seen.append(json.loads(state_path.read_text())["repair_cycle"])
    )

    with repair_cycle._locked_state(args) as state:
        cycle_state, request = _leased(state)
        _dispatch_host(args, state, cycle_state, host, request)

    assert seen[0]["dispatch"]["phase"] == "returned"
    assert seen[0]["dispatch"]["outcome"] == "completed"
    assert (seen[0]["reserved_calls"], seen[0]["consumed_calls"]) == (0, 3)
    assert host.lookups == [("a1", REPOSITORY, Path("."), ("/repo",))]
    recorded = json.loads(state_path.read_text())["repair_cycle"]["dispatch"]
    assert recorded["pull_requests"] == ["https://example.invalid/pull/1"]


def test_dispatch_record_follows_the_outcome() -> None:
    state = _state()
    cycle_state, request = _leased(state)
    parked = _FakeHost(HostOutcome("parked", "missing-host"))
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, parked, request)
    assert cycle_state.dispatch is None
    assert parked.lookups == []

    state = _state()
    cycle_state, request = _leased(state)
    offline = _FakeHost(found=HostLookupError("gh exited 1"))
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, offline, request)
    assert cycle_state.dispatch is not None
    assert (cycle_state.dispatch.phase, cycle_state.dispatch.pull_requests) == ("returned", ())
    assert state["repair_cycle"]["parked_reason"] is None

    state = _state()
    cycle_state, request = _leased(state)
    no_git = _FakeHost(snapshot=HostLookupError("git exited 128"))
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, no_git, request) is None
    assert no_git.admissions == []
    assert (cycle_state.dispatch, cycle_state.reserved_calls) == (None, 0)
    assert state["repair_cycle"]["parked_reason"] == "dispatch-lookup-unavailable"


@pytest.mark.parametrize(
    ("phase", "reserved", "alive", "found", "failure", "parked"),
    [
        ("intent", True, True, None, None, "dispatch-in-flight"),
        ("intent", True, None, None, None, "dispatch-unverified"),
        ("intent", True, False, None, "unsettled-reservation", "unsettled-reservation"),
        ("intent", True, False, "error", "unsettled-reservation", "unsettled-reservation"),
        ("unknown", False, False, None, "dispatch-outcome-unknown", "dispatch-outcome-unknown"),
        ("unknown", False, False, "error", "dispatch-outcome-unknown", "dispatch-outcome-unknown"),
        ("returned", False, False, "error", None, "dispatch-lookup-unavailable"),
        ("returned", False, False, None, None, "already-dispatched"),
    ],
)
def test_replay_never_relaunches(phase, reserved, alive, found, failure, parked) -> None:
    state = _state()
    cycle_state, request = _leased(state)
    recorded = DispatchRecord(
        "a1", phase, pull_requests=("https://example.invalid/pull/0",),
        **({} if phase == "unknown" else {
            "session_id": host_session_id("a1"), "repository": "owner/recorded",
            "repo_root": "/recorded", "prior_worktrees": ("/recorded",),
        }),
    )
    cycle_state.dispatch = recorded
    if reserved:
        cycle_state.admit()
    lookup = HostLookupError("gh exited 1") if found == "error" else HostReferences(
        ("https://example.invalid/pull/1",), ("https://example.invalid/issues/2",), ("/wt",)
    )
    host = _FakeHost(alive=alive, found=lookup)
    late = _args(state, _Client(), now=request.deadline + timedelta(days=1))

    assert _dispatch_host(late, state, cycle_state, host, request) is None

    assert host.admissions == []
    assert (state["repair_cycle"]["attempt_failure"], state["repair_cycle"]["parked_reason"]) == (
        failure,
        parked,
    )
    if alive is not False:
        assert host.lookups == []
        return
    expected = ("a1", REPOSITORY, Path("."), ()) if phase == "unknown" else (
        "a1", "owner/recorded", Path("/recorded"), ("/recorded",)
    )
    assert host.lookups == [expected]
    record = state["repair_cycle"]["dispatch"]
    assert record["pull_requests"][0] == "https://example.invalid/pull/0"
    assert len(record["pull_requests"]) == (1 if found == "error" else 2)
    assert record["worktrees"] == ([] if phase == "unknown" or found == "error" else ["/wt"])
