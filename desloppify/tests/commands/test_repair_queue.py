from __future__ import annotations

import argparse
from contextlib import contextmanager

from desloppify.app.commands.repair_queue import _create_once, cmd_repair_queue
from desloppify.cli import create_parser
from desloppify.engine.repair_queue import (
    GitHubIssue,
    candidate_from_issue,
    concern_key,
    legacy_marker,
)


IDENTITY = "a" * 64
EVIDENCE = "b" * 64
REPOSITORY = "owner/repository"
KEY = concern_key(REPOSITORY, IDENTITY)
BASE = {"key": KEY, "repository": REPOSITORY, "evidence_digest": EVIDENCE}
REVALIDATED = {**BASE, "attestation": "verified current evidence"}


def _state() -> dict:
    return {
        "work_items": {
            "concerns::item": {
                "id": "concerns::item",
                "detector": "concerns",
                "status": "open",
                "detail": {
                    "concern_identity": IDENTITY,
                    "concern_evidence_digest": EVIDENCE,
                },
            }
        }
    }


class _Client:
    def __init__(self) -> None:
        self.searches: list[tuple[str, str]] = []

    def resolve_repository(self, repository: str) -> str:
        assert repository == REPOSITORY
        return repository

    def search(self, repository: str, marker: str):
        self.searches.append((repository, marker))
        return []


def _args(action: str, state: dict, **extra) -> argparse.Namespace:
    values = {
        "command": "repair-queue",
        "repair_queue_action": action,
        "repository": REPOSITORY,
        "state": None,
        "apply": False,
        "attest": None,
        "issue_id": None,
        "marker": None,
        "runtime": None,
        "client": _Client(),
        "state_data": state,
    }
    values.update(extra)
    return argparse.Namespace(**values)


def test_parser_wires_repair_queue_commands() -> None:
    parser = create_parser()
    args = parser.parse_args(["repair-queue", "sync", "--repo", REPOSITORY])
    assert args.command == "repair-queue"
    assert args.repair_queue_action == "sync"
    assert args.repository == REPOSITORY


def test_revalidate_writes_marker_bound_to_repository() -> None:
    state = _state()
    args = _args(
        "revalidate",
        state,
        apply=True,
        issue_id="concerns::item",
        attest="verified current evidence",
    )
    cmd_repair_queue(args)
    record = state["work_items"]["concerns::item"]["detail"]["github_repair_revalidated"]
    assert record == REVALIDATED


def test_sync_dry_run_does_not_create_or_mutate() -> None:
    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_revalidated"] = {
        "marker": legacy_marker(IDENTITY, EVIDENCE),
        "repository": REPOSITORY,
        "attestation": "verified current evidence",
    }
    client = _Client()
    args = _args("sync", state, client=client)
    cmd_repair_queue(args)
    assert client.searches == [(REPOSITORY, KEY)]
    assert "github_repair_pending" not in detail


def test_sync_adopts_closed_match_with_last_read_state() -> None:
    class ClosedMatchClient(_Client):
        def search(self, repository: str, marker: str):
            return [GitHubIssue(7, "https://example.test/7", "closed")]

    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_revalidated"] = {
        "marker": legacy_marker(IDENTITY, EVIDENCE),
        "repository": REPOSITORY,
        "attestation": "verified current evidence",
    }

    cmd_repair_queue(_args("sync", state, apply=True, client=ClosedMatchClient()))

    assert detail["github_repair"] == {
        **BASE,
        "number": 7,
        "url": "https://example.test/7",
        "state": "closed",
    }


def test_delayed_writer_cannot_create_after_another_writer_links() -> None:
    class CreatingClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0

        def create(self, repository: str, candidate) -> None:
            self.create_calls += 1

        def search(self, repository: str, marker: str):
            return [GitHubIssue(7, "https://example.test/7", "open")]

    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_revalidated"] = {
        "marker": legacy_marker(IDENTITY, EVIDENCE),
        "repository": REPOSITORY,
        "attestation": "verified current evidence",
    }
    candidate = candidate_from_issue(state["work_items"]["concerns::item"], REPOSITORY)
    assert candidate is not None
    client = CreatingClient()

    _create_once(_args("sync", state, apply=True, client=client), client, candidate)
    _create_once(_args("sync", state, apply=True, client=client), client, candidate)

    assert client.create_calls == 1
    assert detail["github_repair"]["number"] == 7


def test_uncertain_create_requires_attested_recovery_before_retry() -> None:
    class FailingCreateClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0

        def create(self, repository: str, candidate) -> None:
            self.create_calls += 1
            raise RuntimeError("unavailable")

    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_revalidated"] = dict(REVALIDATED)
    client = FailingCreateClient()

    cmd_repair_queue(_args("sync", state, apply=True, client=client))
    cmd_repair_queue(_args("sync", state, apply=True, client=client))

    assert client.create_calls == 1
    assert detail["github_repair_pending"] == BASE

    cmd_repair_queue(_args("recover", state, apply=True, client=client, marker=KEY, attest="checked"))
    cmd_repair_queue(_args("sync", state, apply=True, client=client))

    assert client.create_calls == 2


def test_recover_clears_only_matching_pending_key() -> None:
    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_pending"] = dict(BASE)
    cmd_repair_queue(_args("recover", state, apply=True, marker="f" * 64, attest="checked GitHub"))
    assert detail["github_repair_pending"] == BASE
    cmd_repair_queue(_args("recover", state, apply=True, marker=KEY, attest="checked GitHub"))
    assert "github_repair_pending" not in detail


def test_recover_accepts_legacy_marker() -> None:
    state = _state()
    marker = legacy_marker(IDENTITY, EVIDENCE)
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_pending"] = {"marker": marker, "repository": REPOSITORY}
    cmd_repair_queue(_args("recover", state, apply=True, marker=marker, attest="checked GitHub"))
    assert "github_repair_pending" not in detail


def test_sync_search_failure_never_attempts_create() -> None:
    class FailingSearchClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.created = False

        def search(self, repository: str, marker: str):
            raise RuntimeError("unavailable")

        def create(self, repository: str, candidate) -> None:
            self.created = True

    state = _state()
    detail = state["work_items"]["concerns::item"]["detail"]
    detail["github_repair_revalidated"] = {
        "marker": legacy_marker(IDENTITY, EVIDENCE),
        "repository": REPOSITORY,
        "attestation": "verified current evidence",
    }
    client = FailingSearchClient()

    cmd_repair_queue(_args("sync", state, apply=True, client=client))

    assert client.created is False
    assert "github_repair_pending" not in detail
