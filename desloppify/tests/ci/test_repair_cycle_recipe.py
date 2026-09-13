from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE = REPO_ROOT / "docs" / "systemd" / "mending-repair-cycle.service"
TIMER = REPO_ROOT / "docs" / "systemd" / "mending-repair-cycle.timer"
GUIDE = REPO_ROOT / "docs" / "systemd" / "repair-cycle.md"


def _settings(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key] = value
    return result


def test_systemd_recipe_is_one_shot_restricted_and_does_not_catch_up() -> None:
    service = _settings(SERVICE)
    timer = _settings(TIMER)

    assert service["Type"] == "oneshot"
    assert service["User"] == "mending"
    assert service["Group"] == "mending"
    assert service["EnvironmentFile"] == "/etc/mending/repair-cycle.env"
    assert service["StateDirectory"] == "mending-repair-cycle"
    assert service["StateDirectoryMode"] == "0700"
    assert service["NoNewPrivileges"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ExecStart"] == (
        "/usr/local/bin/desloppify repair-cycle "
        "--config ${MENDING_REPAIR_CYCLE_CONFIG} "
        "--state /var/lib/mending-repair-cycle/state.json"
    )
    assert "OnCalendar" not in service
    assert "model" not in SERVICE.read_text().lower()
    assert "cost" not in SERVICE.read_text().lower()

    assert timer["OnCalendar"] == "*-*-* 03:00:00"
    assert timer["Persistent"] == "false"
    assert timer["Unit"] == "mending-repair-cycle.service"


def test_recipe_documents_host_timezone_and_restricted_operator_inputs() -> None:
    guide = GUIDE.read_text()
    normalized_guide = guide.replace("\n", " ")

    assert "host system timezone" in normalized_guide
    assert "OnCalendar" in guide
    assert "0640" in guide
    assert "systemctl disable --now mending-repair-cycle.timer" in guide
    assert "parks before selection" in normalized_guide
