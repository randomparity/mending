from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

from desloppify.app.commands.repair_cycle import (
    AdeptReceipt,
    AuthorityProof,
    cmd_repair_cycle,
)
from desloppify.cli import create_parser
from desloppify.engine.repair_cycle import CycleConfig, CycleState

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


def test_terminal_attempt_reconciles_on_restart_without_second_selection() -> None:
    state = _state()
    client = _Client()

    cmd_repair_cycle(_args(state, client))
    cmd_repair_cycle(_args(state, client))

    assert client.selections == 1
    assert client.reconciliations == 1
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
    assert client.reconciliations == 1


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


def test_timeout_and_budget_exhaustion_park_without_accepting_receipt() -> None:
    timed_out_state = _state()
    timed_out_client = _Client()
    timed_out_client.select_error = TimeoutError()

    cmd_repair_cycle(_args(timed_out_state, timed_out_client))

    assert timed_out_state["repair_cycle"]["parked_reason"] == "timeout"

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
