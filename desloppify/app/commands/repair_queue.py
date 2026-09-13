"""One-shot, fail-closed promotion of revalidated concerns."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from desloppify.app.commands.helpers.state import state_path
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import load_state, state_lock
from desloppify.engine.repair_queue import (
    GitHubIssueClient,
    PromotionCandidate,
    candidate_from_issue,
    matching_record,
)


def cmd_repair_queue(args: argparse.Namespace) -> None:
    """Dispatch one repair-queue action without scheduling or execution."""
    client = getattr(args, "client", None) or GitHubIssueClient()
    action = getattr(args, "repair_queue_action", None)
    if action == "revalidate":
        _revalidate(args, client)
    elif action == "recover":
        _recover(args, client)
    elif action == "sync":
        _sync(args, client)
    else:
        raise CommandError("repair-queue requires revalidate, sync, or recover", exit_code=2)


def _revalidate(args: argparse.Namespace, client: Any) -> None:
    _require_apply(args, "revalidate")
    repository = client.resolve_repository(args.repository)
    attestation = _attestation(args)
    with _locked_state(args) as state:
        issue = _issues(state).get(args.issue_id)
        candidate = candidate_from_issue(issue or {}, repository, require_revalidation=False)
        if candidate is None:
            raise CommandError("concern is not current and eligible for revalidation")
        detail = issue["detail"]
        detail["github_repair_revalidated"] = {
            "marker": candidate.marker,
            "repository": repository,
            "attestation": attestation,
        }
    print(f"Revalidated {args.issue_id} for {repository}.")


def _recover(args: argparse.Namespace, client: Any) -> None:
    _require_apply(args, "recover")
    repository = client.resolve_repository(args.repository)
    _attestation(args)
    cleared = 0
    with _locked_state(args) as state:
        for issue in _issues(state).values():
            detail = issue.get("detail") if isinstance(issue, Mapping) else None
            if not isinstance(detail, dict):
                continue
            pending = detail.get("github_repair_pending")
            if isinstance(pending, Mapping) and pending.get("marker") == args.marker and pending.get("repository") == repository:
                detail.pop("github_repair_pending", None)
                cleared += 1
    print(f"Cleared {cleared} pending repair attempt(s).")


def _sync(args: argparse.Namespace, client: Any) -> None:
    repository = args.repository
    state = _read_state(args)
    for issue in _issues(state).values():
        candidate = candidate_from_issue(issue, repository)
        if candidate is None:
            continue
        detail = issue["detail"]
        link = matching_record(detail, "github_repair", candidate)
        if link is not None:
            _read_link(args, client, candidate, link)
            continue
        pending = matching_record(detail, "github_repair_pending", candidate)
        matches = _search(client, candidate)
        if matches is None:
            print(f"Skipped {candidate.issue_id}: GitHub search failed.")
            continue
        if pending is not None:
            _adopt_if_unique(args, candidate, matches, "github_repair_pending")
            continue
        if len(matches) == 1:
            _adopt_if_unique(args, candidate, matches, None)
        elif len(matches) > 1:
            print(f"Skipped {candidate.issue_id}: marker is ambiguous.")
        elif args.apply:
            _create_once(args, client, candidate)
        else:
            print(f"Would create a repair issue for {candidate.issue_id}.")


def _read_link(args: argparse.Namespace, client: Any, candidate: PromotionCandidate, link: Mapping[str, Any]) -> None:
    number = link.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        print(f"Skipped {candidate.issue_id}: linked issue record is malformed.")
        return
    try:
        issue = client.view(candidate.repository, number)
    except (RuntimeError, ValueError):
        print(f"Skipped {candidate.issue_id}: linked issue could not be read.")
        return
    if args.apply:
        _write_link(args, candidate, issue, expected_key="github_repair")
    else:
        print(f"Would reconcile linked issue #{issue.number} for {candidate.issue_id}.")


def _search(client: Any, candidate: PromotionCandidate) -> list[Any] | None:
    try:
        return client.search(candidate.repository, candidate.marker)
    except (RuntimeError, ValueError):
        return None


def _adopt_if_unique(args: argparse.Namespace, candidate: PromotionCandidate, matches: list[Any], expected_key: str | None) -> None:
    if len(matches) != 1:
        print(f"Pending {candidate.issue_id}: no unambiguous GitHub match.")
        return
    if args.apply:
        _write_link(args, candidate, matches[0], expected_key=expected_key)
    else:
        print(f"Would adopt issue #{matches[0].number} for {candidate.issue_id}.")


def _create_once(args: argparse.Namespace, client: Any, candidate: PromotionCandidate) -> None:
    with _locked_state(args) as state:
        fresh = _candidate_by_id(state, candidate.issue_id, candidate.repository)
        detail = _issues(state)[candidate.issue_id]["detail"]
        if (
            fresh != candidate
            or matching_record(detail, "github_repair", candidate)
            or matching_record(detail, "github_repair_pending", candidate)
        ):
            print(f"Skipped {candidate.issue_id}: concern changed while preparing create.")
            return
        detail["github_repair_pending"] = {
            "marker": candidate.marker,
            "repository": candidate.repository,
        }
    try:
        client.create(candidate.repository, candidate)
    except (RuntimeError, ValueError):
        print(f"Pending {candidate.issue_id}: create outcome is uncertain.")
        return
    matches = _search(client, candidate)
    if matches is None:
        print(f"Pending {candidate.issue_id}: post-create search failed.")
        return
    _adopt_if_unique(args, candidate, matches, "github_repair_pending")


def _write_link(args: argparse.Namespace, candidate: PromotionCandidate, issue: Any, *, expected_key: str | None) -> None:
    with _locked_state(args) as state:
        fresh = _candidate_by_id(state, candidate.issue_id, candidate.repository)
        if fresh != candidate:
            print(f"Skipped {candidate.issue_id}: result is stale.")
            return
        detail = _issues(state)[candidate.issue_id]["detail"]
        if expected_key and matching_record(detail, expected_key, candidate) is None:
            print(f"Skipped {candidate.issue_id}: result is stale.")
            return
        detail["github_repair"] = {
            "marker": candidate.marker,
            "repository": candidate.repository,
            "number": issue.number,
            "url": issue.url,
            "state": issue.state,
        }
        detail.pop("github_repair_pending", None)
    print(f"Linked {candidate.issue_id} to issue #{issue.number}.")


def _candidate_by_id(state: Mapping[str, Any], issue_id: str, repository: str) -> PromotionCandidate | None:
    issue = _issues(state).get(issue_id)
    return candidate_from_issue(issue or {}, repository)


def _issues(state: Mapping[str, Any]) -> dict[str, Any]:
    issues = state.get("work_items") or state.get("issues")
    return issues if isinstance(issues, dict) else {}


def _read_state(args: argparse.Namespace) -> dict[str, Any]:
    supplied = getattr(args, "state_data", None)
    return supplied if isinstance(supplied, dict) else load_state(state_path(args))


@contextmanager
def _locked_state(args: argparse.Namespace) -> Iterator[dict[str, Any]]:
    supplied = getattr(args, "state_data", None)
    if isinstance(supplied, dict):
        yield supplied
        return
    with state_lock(state_path(args)) as state:
        yield state


def _require_apply(args: argparse.Namespace, action: str) -> None:
    if not getattr(args, "apply", False):
        raise CommandError(f"repair-queue {action} requires --apply", exit_code=2)


def _attestation(args: argparse.Namespace) -> str:
    value = str(getattr(args, "attest", "") or "").strip()
    if not value:
        raise CommandError("repair-queue requires a non-empty --attest", exit_code=2)
    return value


__all__ = ["cmd_repair_queue"]
