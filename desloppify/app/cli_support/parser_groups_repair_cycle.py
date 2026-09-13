"""Parser wiring for the one-shot external repair-cycle command."""

from __future__ import annotations


def _add_repair_cycle_parser(sub) -> None:
    parser = sub.add_parser("repair-cycle", help="Run one bounded external repair cycle")
    parser.add_argument(
        "--config",
        required=True,
        metavar="PATH",
        help="Restricted JSON configuration for the local repair cycle",
    )
    parser.add_argument("--state", type=str, default=None, help="Path to state file")


__all__ = ["_add_repair_cycle_parser"]
