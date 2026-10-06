from __future__ import annotations

import argparse
import json
import os
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
from desloppify.engine.repair_authority import (
    AuthorityBinding,
    UnsupportedAuthority,
    authority_from_mapping,
    check_authority,
)
from desloppify.engine.repair_brief import reviewed_version
from desloppify.engine.repair_cycle import (
    BudgetAdmission,
    CycleConfig,
    CycleState,
    DispatchRecord,
)
from desloppify.engine.repair_manifest import AnalysisUnknown
from desloppify.engine.repair_queue import candidate_from_issue, concern_key
from desloppify.tests.commands.test_repair_queue import (
    BASE,
    EVIDENCE,
    KEY,
    PASS,
    REPOSITORY,
    _revalidated_state,
    _Source,
)

NOW = datetime(2026, 9, 13, 9, 0, tzinfo=UTC)
ITEM = "concerns::item"


def _version() -> str:
    issue = _revalidated_state()["work_items"][ITEM]
    version = reviewed_version(issue, candidate_from_issue(issue, REPOSITORY))
    assert version is not None
    return version


VERSION = _version()


class _Client:
    def __init__(self) -> None:
        self.reconciliations = 0
        self.selections = 0
        self.bindings: list[AuthorityBinding] = []
        self.receipt: AdeptReceipt | None = None
        self.select_error: Exception | None = None

    def reconcile(self, repository: str, lease) -> AdeptReceipt:
        self.reconciliations += 1
        assert repository == REPOSITORY
        return self._receipt(lease, state="terminal")

    def select(self, config, lease, binding) -> AdeptReceipt:
        self.selections += 1
        assert config.repository == REPOSITORY
        assert isinstance(binding, AuthorityBinding)
        self.bindings.append(binding)
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


def _approval(**overrides: object) -> dict[str, object]:
    approval: dict[str, object] = {
        "key": KEY,
        "reviewed_brief_version": VERSION,
        "evidence_digest": EVIDENCE,
        "files": ["src/impl.py"],
        "actions": ["repair"],
        "call_limit": 100,
        "cost_cap_usd": "2.00",
        "runtime_minutes": 90,
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
    }
    approval.update(overrides)
    return approval


def _authority(revision: str = "r1", **overrides: object) -> dict[str, object]:
    return {"schema": 1, "revision": revision, "approvals": [_approval(**overrides)]}


def _config(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "enabled": True,
        "repository": REPOSITORY,
        "model": "orchestrator-model",
        "cost_cap_usd": "2.00",
        "authority": _authority(),
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
        "source": _Source(),
        "check": lambda issue, manifest: PASS,
        "now": NOW,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


BOUND = {"issue_id": ITEM, "key": KEY, "revision": "r1"}
LINK = {**BASE, "number": 7, "url": "https://example.invalid/issues/7", "state": "open"}


def _state() -> dict:
    return _revalidated_state(github_repair=dict(LINK))


def test_parser_wires_one_shot_repair_cycle() -> None:
    parser = create_parser()
    args = parser.parse_args(["repair-cycle", "--config", "/etc/mending/repair-cycle.json"])

    assert args.command == "repair-cycle"
    assert args.config == "/etc/mending/repair-cycle.json"


def test_terminal_attempt_restarts_without_second_selection() -> None:
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client))
    stale = _Source(AnalysisUnknown("git unavailable"))
    cmd_repair_cycle(_args(state, client, source=stale))

    assert client.selections == 1
    assert client.reconciliations == 0
    assert stale.calls == 0
    assert state["repair_cycle"]["authoritative_receipt"]["state"] == "terminal"
    assert state["repair_cycle"]["parked_reason"] == "daily-attempt-complete"


def test_selection_binds_authority_before_client_selection(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()))

    class _PersistingClient(_Client):
        def select(self, config, lease, binding) -> AdeptReceipt:
            persisted = json.loads(state_path.read_text())["repair_cycle"]
            assert persisted["current_lease"]["attempt_id"] == lease.attempt_id
            assert persisted["authority"] == BOUND
            return super().select(config, lease, binding)

    client = _PersistingClient()
    cmd_repair_cycle(_args(None, client, state=str(state_path)))

    assert client.selections == 1
    binding = client.bindings[0]
    assert (binding.key, binding.reviewed_brief_version, binding.evidence_digest) == (
        KEY,
        VERSION,
        EVIDENCE,
    )
    assert (binding.files, binding.action) == (frozenset({"src/impl.py"}), "repair")


def test_process_lock_allows_only_one_selection_for_concurrent_invocations(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(_state()))

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

    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == "missing-model"


def test_malformed_adapter_results_park_before_attribute_access() -> None:
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

    times = iter((NOW, NOW, NOW + timedelta(minutes=2)))
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
    assert state["repair_cycle"]["current_lease"]["window_start"] == "2026-09-14T00:00"


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


def test_a_merge_receipt_never_consumes_a_permit() -> None:
    state = _state()
    lease = _recorded_attempt(state)
    state["repair_cycle"]["authority"] = BOUND
    client = _Client()
    client.receipt = AdeptReceipt(
        attempt_id=lease.attempt_id,
        state="active",
        reference="pr:7",
        merge_consumed=True,
        calls=1,
        cost_usd=Decimal("0.25"),
        currency="USD",
    )
    cmd_repair_cycle(_args(state, client))

    assert state["repair_cycle"]["parked_reason"] == "merge-permit-exhausted"
    assert "merge_permit_day" not in state["repair_cycle"]


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

    assert (client.reconciliations, client.selections) == (1, 0)
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
    assert args.confirm_stopped is False
    confirmed = create_parser().parse_args(
        ["repair-cycle", "--config", "c.json", "--dispose-attempt", "a1", "--confirm-stopped"]
    )
    assert confirmed.confirm_stopped is True


def test_deadline_timer_interrupts_before_adapter_selection(monkeypatch) -> None:
    def expire_immediately(_which: int, seconds: float) -> tuple[float, float]:
        if seconds:
            repair_cycle._deadline_exceeded(repair_cycle.signal.SIGALRM, None)
        return (0.0, 0.0)

    monkeypatch.setattr(repair_cycle.signal, "setitimer", expire_immediately)
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client))

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
            "authority",
        }
    }

    decoded = CycleState.from_mapping(legacy)
    assert decoded.authority is None
    for malformed in ({"issue_id": ITEM, "key": KEY}, {**BOUND, "revision": 1}, "bound"):
        assert CycleState.from_mapping({**legacy, "authority": malformed}).authority is None

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
            "authority": None,
        }
    ]
    round_trip = CycleState.from_mapping(json.loads(json.dumps(cycle_state.to_mapping())))
    assert round_trip.attempt_history == cycle_state.attempt_history


CONFIG = CycleConfig.from_mapping(_config())


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
    cycle_state.authority = BOUND
    state.update(_state())
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

    args = _args(state, _Client())
    assert _dispatch_host(args, state, cycle_state, CONFIG, host, request) == outcome

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
    assert _dispatch_host(late, state, cycle_state, CONFIG, host, request) is None
    assert state["repair_cycle"]["attempt_failure"] == "runtime-exhausted"
    assert cycle_state.reserved_calls == 0

    state = _state()
    cycle_state, request = _leased(state)
    cycle_state.consumed_calls = 10
    args = _args(state, _Client())
    assert _dispatch_host(args, state, cycle_state, CONFIG, host, request) is None
    assert state["repair_cycle"]["attempt_failure"] == "budget-exhausted"
    assert host.admissions == []

    for mismatched in (
        replace(request, attempt_id="other"),
        replace(request, deadline=request.deadline + timedelta(seconds=1)),
    ):
        with pytest.raises(ValueError, match="current lease"):
            _dispatch_host(_args(state, _Client()), state, cycle_state, CONFIG, host, mismatched)


def test_dispatch_persists_reservation_before_launch(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    args = _args(None, _Client(), state=str(state_path))
    seen: list[dict] = []

    def read_disk() -> None:
        seen.append(json.loads(state_path.read_text())["repair_cycle"])

    with repair_cycle._locked_state(args) as state:
        cycle_state, request = _leased(state)
        _dispatch_host(args, state, cycle_state, CONFIG, _FakeHost(on_run=read_disk), request)

    assert (seen[0]["reserved_cost_usd"], seen[0]["reserved_calls"]) == ("2.00", 10)
    assert seen[0]["dispatch"] == DispatchRecord(
        "a1", "intent", host_session_id("a1"), REPOSITORY, str(Path(".").resolve()), ("/repo",)
    ).to_mapping()
    settled = json.loads(state_path.read_text())["repair_cycle"]
    assert (settled["reserved_calls"], settled["consumed_calls"]) == (0, 3)
    assert settled["consumed_cost_usd"] == "0.5"


def test_interrupted_dispatch_keeps_reservation(tmp_path) -> None:
    state_path = tmp_path / "state.json"
    args = _args(None, _Client(), state=str(state_path))

    with pytest.raises(KeyboardInterrupt), repair_cycle._locked_state(args) as state:
        cycle_state, request = _leased(state)
        interrupted = _FakeHost(error=KeyboardInterrupt())
        _dispatch_host(args, state, cycle_state, CONFIG, interrupted, request)

    host = _FakeHost()
    after_deadline = _args(None, _Client(), state=str(state_path), now=request.deadline)
    with repair_cycle._locked_state(after_deadline) as state:
        restarted = repair_cycle._cycle_state(state)
        assert (restarted.reserved_cost_usd, restarted.reserved_calls) == (Decimal("2.00"), 10)
        assert _dispatch_host(after_deadline, state, restarted, CONFIG, host, request) is None

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
        _dispatch_host(args, state, cycle_state, CONFIG, host, request)

    assert seen[0]["dispatch"]["phase"] == "returned"
    assert seen[0]["dispatch"]["outcome"] == "completed"
    assert (seen[0]["reserved_calls"], seen[0]["consumed_calls"]) == (0, 3)
    assert host.lookups == [("a1", REPOSITORY, Path(".").resolve(), ("/repo",))]
    recorded = json.loads(state_path.read_text())["repair_cycle"]["dispatch"]
    assert recorded["pull_requests"] == ["https://example.invalid/pull/1"]


def test_dispatch_record_follows_the_outcome() -> None:
    state = _state()
    cycle_state, request = _leased(state)
    parked = _FakeHost(HostOutcome("parked", "missing-host"))
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, CONFIG, parked, request)
    assert cycle_state.dispatch is None
    assert parked.lookups == []

    state = _state()
    cycle_state, request = _leased(state)
    offline = _FakeHost(found=HostLookupError("gh exited 1"))
    assert _dispatch_host(_args(state, _Client()), state, cycle_state, CONFIG, offline, request)
    assert cycle_state.dispatch is not None
    assert (cycle_state.dispatch.phase, cycle_state.dispatch.pull_requests) == ("returned", ())
    assert state["repair_cycle"]["parked_reason"] is None

    state = _state()
    cycle_state, request = _leased(state)
    no_git = _FakeHost(snapshot=HostLookupError("git exited 128"))
    args = _args(state, _Client())
    assert _dispatch_host(args, state, cycle_state, CONFIG, no_git, request) is None
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

    assert _dispatch_host(late, state, cycle_state, CONFIG, host, request) is None

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


def _binding(**overrides: object) -> AuthorityBinding:
    values: dict[str, object] = {
        "repository": REPOSITORY,
        "key": KEY,
        "reviewed_brief_version": VERSION,
        "evidence_digest": EVIDENCE,
        "files": frozenset({"src/impl.py"}),
        "action": "repair",
        "call_limit": 100,
        "cost_cap_usd": Decimal("2.00"),
        "runtime_seconds": 90 * 60,
    }
    values.update(overrides)
    return AuthorityBinding(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("authority", "binding", "bound", "reason"),
    [
        (_authority(), {}, False, None),
        (_authority("r2"), {}, True, None),
        (None, {}, False, "authority-missing"),
        (_authority(key="other"), {}, False, "authority-missing"),
        (None, {}, True, "authority-revoked"),
        (_authority(key="other"), {}, True, "authority-revoked"),
        (_authority(revoked=True), {}, False, "authority-revoked"),
        (_authority(), {"repository": "other/repository"}, False, "authority-mismatch"),
        (_authority(), {"reviewed_brief_version": "f" * 64}, False, "authority-mismatch"),
        (_authority(), {"evidence_digest": "f" * 64}, False, "authority-mismatch"),
        (_authority(expires_at=NOW.isoformat()), {}, False, "authority-expired"),
        (_authority(), {"action": "merge"}, False, "authority-scope-exceeded"),
        (_authority(), {"files": frozenset({"src/impl.py", "x.py"})}, False,
         "authority-scope-exceeded"),
        (_authority(), {"call_limit": 101}, False, "authority-limits-exceeded"),
        (_authority(), {"cost_cap_usd": Decimal("2.01")}, False, "authority-limits-exceeded"),
        (_authority(), {"runtime_seconds": 90 * 60 + 1}, False, "authority-limits-exceeded"),
    ],
)
def test_check_authority_reasons(authority, binding, bound, reason) -> None:
    decoded = authority_from_mapping(REPOSITORY, authority)

    assert check_authority(decoded, _binding(**binding), NOW, bound) == reason


def test_authority_decoding() -> None:
    assert authority_from_mapping(REPOSITORY, None) is None
    with pytest.raises(UnsupportedAuthority):
        authority_from_mapping(REPOSITORY, {**_authority(), "schema": 2})
    with pytest.raises(UnsupportedAuthority):
        authority_from_mapping(REPOSITORY, _authority(actions=["repair", "merge"]))
    duplicate = {**_authority(), "approvals": [_approval(), _approval()]}
    for bad in (
        "authority",
        {**_authority(), "approvals": {}},
        {**_authority(), "revision": ""},
        duplicate,
        _authority(expires_at="2026-09-13T09:00:00"),
        _authority(expires_at="soon"),
        _authority(files=[]),
        _authority(call_limit=True),
        _authority(cost_cap_usd="0"),
        _authority(cost_cap_usd=1.5),
        _authority(revoked="no"),
    ):
        with pytest.raises(ValueError):
            authority_from_mapping(REPOSITORY, bad)


@pytest.mark.parametrize(
    ("state", "overrides", "reason"),
    [
        (_revalidated_state(), {}, "selected-repair-unavailable"),
        (_revalidated_state(github_repair={**LINK, "state": "closed"}), {},
         "selected-repair-unavailable"),
        (None, {"config_data": _config(authority="yes")}, "authority-invalid"),
        (None, {"config_data": _config(authority={**_authority(), "schema": 2})},
         "authority-unsupported"),
        (None, {"config_data": _config(authority=None)}, "authority-missing"),
        (None, {"config_data": _config(authority=_authority(reviewed_brief_version="f" * 64))},
         "authority-mismatch"),
        (None, {"config_data": _config(authority=_authority(revoked=True))}, "authority-revoked"),
        (None, {"config_data": _config(authority=_authority(expires_at=NOW.isoformat()))},
         "authority-expired"),
        (None, {"config_data": _config(authority=_authority(files=["src/other.py"]))},
         "authority-scope-exceeded"),
        (None, {"config_data": _config(call_limit=500)}, "authority-limits-exceeded"),
        (None, {"source": _Source(AnalysisUnknown("git unavailable"))}, "source-unreadable"),
        (None, {"check": lambda issue, manifest: replace(PASS, outcome="fail")},
         "source-not-current"),
    ],
)
def test_selection_parks_without_authority(state, overrides, reason) -> None:
    state = state or _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client, **overrides))

    assert client.selections == 0
    assert state["repair_cycle"]["parked_reason"] == reason
    assert state["repair_cycle"]["authority"] is None


@pytest.mark.parametrize(
    ("foreign_owner", "writable", "reason", "selections"),
    [
        (False, set(), "authority-untrusted", 0),
        (True, {"file"}, "authority-untrusted", 0),
        (True, {"parent"}, "authority-untrusted", 0),
        (True, {"ancestor"}, "authority-untrusted", 0),
        (True, set(), None, 1),
    ],
)
def test_only_a_foreign_read_only_config_grants_authority(
    tmp_path, monkeypatch, foreign_owner, writable, reason, selections
) -> None:
    path = (tmp_path / "etc" / "repair-cycle.json").resolve()
    path.parent.mkdir()
    path.write_text(json.dumps(_config()))
    named = {"file": path, "parent": path.parent, "ancestor": path.parent.parent}
    writable_paths = {named[name] for name in writable}
    if foreign_owner:
        monkeypatch.setattr(repair_cycle.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(repair_cycle.os, "access", lambda entry, _mode: entry in writable_paths)
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client, config=str(path), config_data=None))

    assert client.selections == selections
    assert state["repair_cycle"]["parked_reason"] == reason


def test_missing_or_symlinked_config_is_untrusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(repair_cycle.os, "geteuid", lambda: os.getuid() + 1)
    monkeypatch.setattr(repair_cycle.os, "access", lambda _entry, _mode: False)
    target = (tmp_path / "repair-cycle.json").resolve()
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    def trusted(path: Path) -> bool:
        return repair_cycle._trusted_config(argparse.Namespace(config=str(path), config_data=None))

    (tmp_path / "sub").mkdir()
    assert trusted(target) is True
    assert trusted(link) is False
    assert trusted(tmp_path / "sub" / ".." / "repair-cycle.json") is False
    assert trusted(tmp_path / "gone.json") is False


def test_dispatch_authority_check_is_time_bounded(monkeypatch) -> None:
    state = _state()
    cycle_state, request = _leased(state)
    host = _FakeHost()

    def overdue(*_args, **_kwargs):
        raise TimeoutError("repair-cycle call bound exceeded")

    monkeypatch.setattr(repair_cycle, "source_comparison", overdue)
    args = _args(state, _Client())
    assert _dispatch_host(args, state, cycle_state, CONFIG, host, request) is None

    assert host.admissions == []
    assert state["repair_cycle"]["parked_reason"] == "timeout"


@pytest.mark.parametrize(
    ("bound", "config", "reason"),
    [
        (False, _config(), "authority-missing"),
        (True, _config(authority=_authority(revoked=True)), "authority-revoked"),
        (True, _config(authority=None), "authority-revoked"),
        (True, _config(authority=_authority(key="other")), "authority-revoked"),
        (True, _config(authority=_authority(expires_at=NOW.isoformat())), "authority-expired"),
        (True, _config(cost_cap_usd="1.00", authority=_authority(cost_cap_usd="1.00")),
         "authority-limits-exceeded"),
    ],
)
def test_dispatch_rechecks_authority(bound, config, reason) -> None:
    state = _state()
    cycle_state, request = _leased(state)
    if not bound:
        cycle_state.authority = None
    host = _FakeHost()

    result = _dispatch_host(
        _args(state, _Client()), state, cycle_state, CycleConfig.from_mapping(config), host, request
    )

    assert result is None
    assert (host.admissions, host.lookups, cycle_state.dispatch) == ([], [], None)
    assert state["repair_cycle"]["parked_reason"] == reason
    assert state["repair_cycle"]["reserved_calls"] == 0


def test_dispatch_rechecks_source() -> None:
    state = _state()
    cycle_state, request = _leased(state)
    host = _FakeHost()
    stale = _args(state, _Client(), source=_Source(AnalysisUnknown("git unavailable")))

    assert _dispatch_host(stale, state, cycle_state, CONFIG, host, request) is None

    assert host.admissions == []
    assert state["repair_cycle"]["parked_reason"] == "source-unreadable"


class _ActiveClient(_Client):
    def reconcile(self, repository: str, lease) -> AdeptReceipt:
        self.reconciliations += 1
        return self._receipt(lease, state="active")


def _bound_attempt(state: dict) -> None:
    _recorded_attempt(state)
    state["repair_cycle"]["authority"] = BOUND


@pytest.mark.parametrize(
    ("config", "failure"),
    [
        (_config(), None),
        (_config(authority=_authority("r2")), None),
        (_config(authority=_authority(revoked=True)), "authority-revoked"),
        (_config(authority=_authority(key="other")), "authority-revoked"),
    ],
)
def test_resume_rechecks_an_active_attempt(config, failure) -> None:
    state = _state()
    _bound_attempt(state)
    client = _ActiveClient()

    cmd_repair_cycle(_args(state, client, config_data=config))

    recorded = state["repair_cycle"]
    assert (client.reconciliations, client.selections) == (1, 0)
    assert recorded["authoritative_receipt"]["state"] == "active"
    assert (recorded["attempt_failure"], recorded["parked_reason"]) == (failure, failure)


def test_resume_of_a_changed_brief_fails_the_attempt() -> None:
    state = _state()
    _bound_attempt(state)
    state["work_items"][ITEM]["detail"]["verification"] = "Run a different suite"

    cmd_repair_cycle(_args(state, _ActiveClient()))

    assert state["repair_cycle"]["attempt_failure"] == "authority-mismatch"


def test_dispose_refuses_an_attempt_last_reported_active() -> None:
    state = _state()
    _bound_attempt(state)
    client = _ActiveClient()
    cmd_repair_cycle(_args(state, client, config_data=_config(authority=_authority(revoked=True))))
    assert state["repair_cycle"]["attempt_failure"] == "authority-revoked"

    with pytest.raises(CommandError, match="--confirm-stopped"):
        cmd_repair_cycle(_args(state, client, dispose_attempt="recorded-attempt"))
    assert state["repair_cycle"]["disposed_attempt"] is None

    cmd_repair_cycle(
        _args(state, client, dispose_attempt="recorded-attempt", confirm_stopped=True)
    )
    assert state["repair_cycle"]["disposed_attempt"] == "recorded-attempt"


@pytest.mark.parametrize(
    "dispatch",
    [
        DispatchRecord("a1", "intent"),
        DispatchRecord("a1", "returned", outcome="unknown"),
    ],
)
def test_dispose_refuses_an_unsettled_dispatch(dispatch) -> None:
    cycle_state = _budget_state()
    cycle_state.dispatch = dispatch
    cycle_state.fail("host-error")

    with pytest.raises(ValueError, match="--confirm-stopped"):
        cycle_state.dispose("a1")
    cycle_state.dispose("a1", confirm_stopped=True)
    assert cycle_state.disposed


def test_resume_of_an_unbound_active_attempt_fails() -> None:
    state = _state()
    _recorded_attempt(state)

    cmd_repair_cycle(_args(state, _ActiveClient()))

    assert state["repair_cycle"]["attempt_failure"] == "authority-missing"


def test_resume_of_a_rebound_work_item_is_a_mismatch() -> None:
    state = _state()
    _bound_attempt(state)
    state["repair_cycle"]["authority"] = {**BOUND, "key": "f" * 64}

    cmd_repair_cycle(_args(state, _ActiveClient()))

    assert state["repair_cycle"]["attempt_failure"] == "authority-mismatch"


def test_selection_prefers_an_approved_published_repair() -> None:
    state = _state()
    other_key = concern_key(REPOSITORY, "d" * 64)
    assert other_key < KEY  # ranks first on the key tie-break unless approval decides
    other = json.loads(json.dumps(state["work_items"][ITEM]))
    other["id"] = "concerns::other"
    other["detail"]["concern_identity"] = "d" * 64
    for record in ("github_repair_revalidated", "github_repair"):
        other["detail"][record]["key"] = other_key
    state["work_items"]["concerns::other"] = other
    client = _Client()

    cmd_repair_cycle(_args(state, client))

    assert client.bindings[0].key == KEY
    assert state["repair_cycle"]["authority"] == BOUND


def test_resume_parks_on_unreadable_source_without_failing() -> None:
    state = _state()
    _bound_attempt(state)
    unreadable = _Source(AnalysisUnknown("git unavailable"))

    cmd_repair_cycle(_args(state, _ActiveClient(), source=unreadable))

    assert state["repair_cycle"]["parked_reason"] == "source-unreadable"
    assert state["repair_cycle"]["attempt_failure"] is None


def test_config_authority_is_copied_from_the_source_mapping() -> None:
    raw = _config()
    config = CycleConfig.from_mapping(raw)
    raw["authority"]["approvals"].append(_approval(key="other"))  # type: ignore[index]

    assert config.authority == _config()["authority"]
