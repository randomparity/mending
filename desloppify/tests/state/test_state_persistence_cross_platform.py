"""Regression tests for cross-platform state persistence."""

from __future__ import annotations

import errno
import importlib
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from desloppify.base.exception_sets import CommandError


def test_state_lock_imports_and_round_trips_on_current_platform(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")

    state_path = tmp_path / "state.json"
    with persistence_mod.state_lock(state_path) as state:
        state["scan_count"] = 3

    loaded = persistence_mod.load_state(state_path)
    assert loaded["scan_count"] == 3


def test_state_lock_times_out_without_bad_file_descriptor(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"

    with patch.object(
        persistence_mod,
        "_acquire_state_lock",
        side_effect=OSError(errno.EACCES, "busy"),
    ):
        with pytest.raises(TimeoutError, match="Could not acquire state lock"):
            with persistence_mod.state_lock(state_path, timeout=0.0):
                pass


def _locked_write(persistence_mod, state_path):
    with persistence_mod.state_lock(state_path) as state:
        state["scan_count"] = 99


def test_state_lock_refuses_to_save_over_state_with_invalid_invariants(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    original = json.dumps({"version": 2, "work_items": {"a": {"id": "b"}}}).encode()
    state_path.write_bytes(original)

    for _ in range(2):
        with pytest.raises(CommandError, match=re.escape(str(state_path))) as excinfo:
            _locked_write(persistence_mod, state_path)

    assert "invariants" in excinfo.value.message
    assert "scan" in excinfo.value.message
    assert state_path.read_bytes() == original
    assert not state_path.with_suffix(".json.bak").exists()


def test_state_lock_keeps_undecodable_state_without_backup(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    original = b"{not json"
    state_path.write_bytes(original)

    with pytest.raises(CommandError, match=re.escape(str(state_path))) as excinfo:
        _locked_write(persistence_mod, state_path)
    assert "state.json.corrupted" in excinfo.value.message
    assert "scan" in excinfo.value.message

    with pytest.raises(CommandError, match="state.json.corrupted"):
        _locked_write(persistence_mod, state_path)

    assert not state_path.exists()
    assert state_path.with_suffix(".json.corrupted").read_bytes() == original


def test_state_lock_refuses_after_unlocked_load_moved_undecodable_state(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    original = b"{not json"
    state_path.write_bytes(original)

    persistence_mod.load_state(state_path)

    with pytest.raises(CommandError, match=re.escape(str(state_path))):
        _locked_write(persistence_mod, state_path)
    assert not state_path.exists()
    assert state_path.with_suffix(".json.corrupted").read_bytes() == original


def test_state_lock_saves_state_recovered_from_backup(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"{not json")
    state_path.with_suffix(".json.bak").write_text(json.dumps({"version": 2, "scan_count": 4}))

    with persistence_mod.state_lock(state_path) as state:
        assert state["scan_count"] == 4
        state["scan_count"] = 5

    assert json.loads(state_path.read_text())["scan_count"] == 5


def test_state_lock_saves_state_reconstructed_from_saved_plan(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    issue_id = "review::src/foo.ts::abcd1234"
    plan = {
        "queue_order": [issue_id],
        "clusters": {},
        "epic_triage_meta": {"triage_stages": {"observe": {"report": "done"}}},
        "skipped": {},
    }
    (tmp_path / "plan.json").write_text(json.dumps(plan))

    with persistence_mod.state_lock(state_path) as state:
        assert issue_id in state["work_items"]

    assert issue_id in json.loads(state_path.read_text())["work_items"]


def test_state_lock_refuses_undecodable_state_it_cannot_move_aside(tmp_path):
    persistence_mod = importlib.import_module("desloppify.engine._state.persistence")
    state_path = tmp_path / "state.json"
    original = b"{not json"
    state_path.write_bytes(original)

    with patch.object(Path, "rename", side_effect=OSError(errno.EACCES, "denied")):
        for _ in range(2):
            with pytest.raises(CommandError, match=re.escape(str(state_path))):
                _locked_write(persistence_mod, state_path)

    assert state_path.read_bytes() == original
    assert not state_path.with_suffix(".json.bak").exists()
