"""Parser wiring for the one-shot GitHub repair queue."""

from __future__ import annotations

import argparse


def _add_repair_queue_parser(sub) -> None:
    parser = sub.add_parser("repair-queue", help="Promote revalidated concerns to GitHub")
    actions = parser.add_subparsers(dest="repair_queue_action", required=True)
    for action in ("sync", "revalidate", "recover"):
        child = actions.add_parser(action)
        child.add_argument("--repo", dest="repository", required=True, metavar="OWNER/REPO")
        child.add_argument("--state", type=str, default=None, help="Path to state file")
        child.add_argument("--apply", action="store_true", help="Permit local or GitHub writes")
        child.add_argument("--attest", type=str, default=None, metavar="TEXT")
    actions.choices["revalidate"].add_argument("issue_id")
    actions.choices["recover"].add_argument("marker")


__all__ = ["_add_repair_queue_parser"]
