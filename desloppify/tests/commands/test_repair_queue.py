from __future__ import annotations

import argparse

import pytest

from desloppify.app.commands.repair_queue import _create_once, cmd_repair_queue
from desloppify.cli import create_parser
from desloppify.engine._state.merge_issues import upsert_issues
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
    assert client.searches == [(REPOSITORY, KEY), (REPOSITORY, IDENTITY)]
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


ISSUE_7 = GitHubIssue(7, "https://example.test/7", "closed")
LINK_7 = {**BASE, "number": 7, "url": "https://example.test/7", "state": "closed"}


class _Recorder(_Client):
    """Fake gh: per-term search results, by-number views, counted creates."""

    def __init__(self, results: dict[str, list] | None = None, viewed=ISSUE_7) -> None:
        super().__init__()
        self.results = results or {}
        self.viewed = viewed
        self.views: list[int] = []
        self.create_calls = 0

    def search(self, repository: str, term: str):
        self.searches.append((repository, term))
        return list(self.results.get(term, []))

    def view(self, repository: str, number: int):
        self.views.append(number)
        return self.viewed

    def create(self, repository: str, candidate) -> None:
        self.create_calls += 1


def _item(issue_id: str, *, status: str = "open", evidence: str = EVIDENCE, **records) -> dict:
    detail = {"concern_identity": IDENTITY, "concern_evidence_digest": evidence, **records}
    return {"id": issue_id, "detector": "concerns", "status": status, "detail": detail}


def _revalidated_state(**records) -> dict:
    state = _state()
    state["work_items"]["concerns::item"]["detail"].update(
        github_repair_revalidated=dict(REVALIDATED), **records
    )
    return state


def _sync(state: dict, client: _Recorder, *, apply: bool = True) -> None:
    cmd_repair_queue(_args("sync", state, apply=apply, client=client))


def _detail(state: dict, issue_id: str = "concerns::item") -> dict:
    return state["work_items"][issue_id]["detail"]


def test_sync_searches_key_and_identity_digest() -> None:
    client = _Recorder()
    _sync(_revalidated_state(), client, apply=False)
    assert client.searches == [(REPOSITORY, KEY), (REPOSITORY, IDENTITY)]
    assert client.create_calls == 0


def test_evidence_change_reconciles_closed_link_without_create() -> None:
    new_evidence = "c" * 64
    old_link = {**LINK_7, "evidence_digest": EVIDENCE}
    state = {"work_items": {"concerns::item": _item(
        "concerns::item",
        evidence=new_evidence,
        github_repair=old_link,
        github_repair_revalidated={**BASE, "evidence_digest": new_evidence, "attestation": "fresh review"},
    )}}
    client = _Recorder()
    _sync(state, client)
    assert client.views == [7]
    assert client.searches == []
    assert client.create_calls == 0
    assert _detail(state)["github_repair"] == {**LINK_7, "evidence_digest": new_evidence}


def test_old_evidence_revalidation_is_not_eligible() -> None:
    state = {"work_items": {"concerns::item": _item(
        "concerns::item", evidence="c" * 64, github_repair=dict(LINK_7),
        github_repair_revalidated=dict(REVALIDATED),
    )}}
    client = _Recorder()
    _sync(state, client)
    assert (client.views, client.searches, client.create_calls) == ([], [], 0)


def test_evidence_change_round_trip_reuses_link() -> None:
    new_evidence = "c" * 64
    stored = _item("concerns::item", github_repair=dict(LINK_7), github_repair_revalidated=dict(REVALIDATED))
    stored.update(file=".", tier=2, confidence="high", summary="concern", suppressed=False)
    state = {"work_items": {"concerns::item": stored}}
    scanned = {**stored, "detail": {"concern_identity": IDENTITY, "concern_evidence_digest": new_evidence}}
    upsert_issues(state["work_items"], [scanned], [], "2026-10-05T00:00:00Z", lang=None)
    upsert_issues(state["work_items"], [scanned], [], "2026-10-06T00:00:00Z", lang=None)
    assert "previous_concern_evidence_digest" not in _detail(state)
    client = _Recorder()
    cmd_repair_queue(_args("revalidate", state, apply=True, client=client, issue_id="concerns::item", attest="fresh"))
    _sync(state, client)
    assert client.views == [7]
    assert client.create_calls == 0
    assert _detail(state)["github_repair"]["state"] == "closed"
    assert _detail(state)["github_repair"]["evidence_digest"] == new_evidence


def test_legacy_link_is_migrated_on_sync() -> None:
    legacy = {
        "marker": legacy_marker(IDENTITY, EVIDENCE), "repository": REPOSITORY,
        "number": 7, "url": "https://example.test/7", "state": "open",
    }
    state = _revalidated_state(github_repair=legacy)
    client = _Recorder()
    _sync(state, client)
    assert client.views == [7]
    assert _detail(state)["github_repair"] == LINK_7


def test_legacy_pending_never_creates_again() -> None:
    pending = {"marker": legacy_marker(IDENTITY, EVIDENCE), "repository": REPOSITORY}
    state = _revalidated_state(github_repair_pending=pending)
    client = _Recorder()
    _sync(state, client)
    _sync(state, client)
    assert client.create_calls == 0
    assert _detail(state)["github_repair_pending"] == pending
    client.results = {IDENTITY: [ISSUE_7]}
    _sync(state, client)
    assert client.create_calls == 0
    assert _detail(state)["github_repair"] == LINK_7
    assert "github_repair_pending" not in _detail(state)


@pytest.mark.parametrize(
    "records",
    [
        {"github_repair": {**LINK_7, "repository": "other/repository"}},
        {"github_repair_pending": {**BASE, "key": "f" * 64}},
    ],
)
def test_unrecognized_record_parks_before_github(records: dict) -> None:
    state = _revalidated_state(**records)
    client = _Recorder()
    _sync(state, client)
    assert (client.views, client.searches, client.create_calls) == ([], [], 0)


def test_ambiguous_key_parks_all_candidates() -> None:
    state = _revalidated_state()
    state["work_items"]["concerns::renamed"] = _item("concerns::renamed", github_repair_revalidated=dict(REVALIDATED))
    client = _Recorder()
    _sync(state, client)
    assert (client.searches, client.create_calls) == ([], 0)


def test_rename_adopts_local_link_from_old_item() -> None:
    state = _revalidated_state()
    state["work_items"]["concerns::old"] = _item("concerns::old", status="fixed", github_repair=dict(LINK_7))
    client = _Recorder()
    _sync(state, client)
    assert client.views == [7]
    assert (client.searches, client.create_calls) == ([], 0)
    assert _detail(state)["github_repair"] == LINK_7


def test_rename_adopts_legacy_issue_found_by_identity() -> None:
    state = _revalidated_state()
    client = _Recorder({IDENTITY: [ISSUE_7]})
    _sync(state, client)
    assert client.create_calls == 0
    assert _detail(state)["github_repair"] == LINK_7


def test_rename_with_peer_pending_never_creates() -> None:
    state = _revalidated_state()
    state["work_items"]["concerns::old"] = _item("concerns::old", status="fixed", github_repair_pending=dict(BASE))
    client = _Recorder()
    _sync(state, client)
    assert (client.searches, client.create_calls) == ([], 0)
    cmd_repair_queue(_args("recover", state, apply=True, client=client, marker=KEY, attest="checked GitHub"))
    _sync(state, client)
    assert client.create_calls == 1


def test_rename_with_corrupt_peer_link_parks() -> None:
    state = _revalidated_state()
    state["work_items"]["concerns::old"] = _item(
        "concerns::old", status="fixed", github_repair={**LINK_7, "number": "seven"}
    )
    client = _Recorder()
    _sync(state, client)
    assert (client.views, client.searches, client.create_calls) == ([], [], 0)


def test_conflicting_peer_links_park() -> None:
    state = _revalidated_state()
    state["work_items"]["concerns::old"] = _item("concerns::old", status="fixed", github_repair=dict(LINK_7))
    state["work_items"]["concerns::older"] = _item(
        "concerns::older", status="fixed", github_repair={**LINK_7, "number": 8, "url": "https://example.test/8"}
    )
    client = _Recorder()
    _sync(state, client)
    assert (client.views, client.searches, client.create_calls) == ([], [], 0)


def test_multiple_github_matches_park() -> None:
    state = _revalidated_state()
    client = _Recorder({KEY: [ISSUE_7], IDENTITY: [GitHubIssue(8, "https://example.test/8", "open")]})
    _sync(state, client)
    assert client.create_calls == 0
    assert "github_repair" not in _detail(state)


def test_human_edited_issue_reconciles_by_number() -> None:
    state = _revalidated_state(github_repair={**LINK_7, "state": "open"})
    client = _Recorder()
    _sync(state, client)
    assert client.views == [7]
    assert (client.searches, client.create_calls) == ([], 0)
    assert _detail(state)["github_repair"]["state"] == "closed"


def test_create_refuses_when_key_becomes_ambiguous_under_lock() -> None:
    state = _revalidated_state()
    candidate = candidate_from_issue(state["work_items"]["concerns::item"], REPOSITORY)
    assert candidate is not None
    state["work_items"]["concerns::renamed"] = _item("concerns::renamed", github_repair_revalidated=dict(REVALIDATED))
    client = _Recorder()
    _create_once(_args("sync", state, apply=True, client=client), client, candidate)
    assert client.create_calls == 0
    assert "github_repair_pending" not in _detail(state)


def test_successful_create_links_new_shape_record() -> None:
    class CreateThenFind(_Recorder):
        def create(self, repository: str, candidate) -> None:
            super().create(repository, candidate)
            self.results = {KEY: [GitHubIssue(9, "https://example.test/9", "open")]}

    state = _revalidated_state()
    client = CreateThenFind()
    _sync(state, client)
    assert client.create_calls == 1
    assert _detail(state)["github_repair"] == {**BASE, "number": 9, "url": "https://example.test/9", "state": "open"}
    assert "github_repair_pending" not in _detail(state)
