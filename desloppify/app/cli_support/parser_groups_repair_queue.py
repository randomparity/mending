"""Parser wiring for the one-shot GitHub repair queue."""

from __future__ import annotations

_SYNC_HELP = (
    "Reconcile linked issues, then publish at most one ranked small repair (a"
    " high-confidence concern of at most 3 files in one directory, or an exact"
    " same-file duplicate pair); records a no-op when none is safe. Proposals"
    " publish without status:ready and are never dispatched."
)


def _add_repair_queue_parser(sub) -> None:
    parser = sub.add_parser(
        "repair-queue",
        help="Publish revalidated small repairs (one per sync) and non-dispatchable proposals"
        " to GitHub",
    )
    actions = parser.add_subparsers(dest="repair_queue_action", required=True)
    for action in ("sync", "revalidate", "recover"):
        texts = {"help": _SYNC_HELP, "description": _SYNC_HELP} if action == "sync" else {}
        child = actions.add_parser(action, **texts)
        child.add_argument("--repo", dest="repository", required=True, metavar="OWNER/REPO")
        child.add_argument("--state", type=str, default=None, help="Path to state file")
        child.add_argument("--apply", action="store_true", help="Permit local or GitHub writes")
    for action in ("sync", "revalidate"):
        child = actions.choices[action]
        child.add_argument(
            "--source-root", default=None, metavar="PATH",
            help="Repository top level to read source from (default: project root)",
        )
        child.add_argument(
            "--revision", default="HEAD", help="Revision to read source at (default: HEAD)"
        )
    actions.choices["revalidate"].add_argument("issue_id")
    actions.choices["recover"].add_argument("marker")


__all__ = ["_add_repair_queue_parser"]
