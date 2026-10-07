"""The process-creation guard behind ``scan --no-external-tools`` (#75)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from desloppify.base.process_guard import (
    ExternalToolRefused,
    deny_process_creation,
    process_creation_denied,
    refusal_count,
)


def test_guard_refuses_process_creation_only_while_open(tmp_path) -> None:
    marker = tmp_path / "ran"
    start = refusal_count()
    with deny_process_creation():
        assert process_creation_denied()
        with pytest.raises(ExternalToolRefused):
            subprocess.run([sys.executable, "-c", f"open({str(marker)!r}, 'w')"], check=False)
        with pytest.raises(ExternalToolRefused):
            os.system(f"touch {marker}")
        with pytest.raises(ExternalToolRefused):
            os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", "pass"])
    assert refusal_count() >= start + 3
    assert not marker.exists()
    assert not process_creation_denied()
    assert subprocess.run([sys.executable, "-c", "pass"], check=False).returncode == 0
