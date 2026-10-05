"""One-shot, fail-closed promotion of revalidated concerns."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from desloppify.app.commands.helpers.state import state_path
from desloppify.base.discovery.paths import get_project_root
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import load_state, state_lock
from desloppify.engine.repair_brief import ParkedBrief, build_brief, render_brief
from desloppify.engine.repair_manifest import (
    ManifestComparison,
    SourceCheckout,
    SourceManifest,
    compare_manifests,
    manifest_from_record,
)
from desloppify.engine.repair_queue import (
    GitHubIssueClient,
    PromotionCandidate,
    RepairRecordError,
    candidate_from_issue,
    carries_concern_marker,
    concern_hashes,
    concern_key,
    legacy_marker,
    matching_record,
    normalize_record,
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
    with _locked_state(args) as state:
        issue = _issues(state).get(args.issue_id)
        candidate = candidate_from_issue(issue or {}, repository, require_revalidation=False)
        if candidate is None:
            raise CommandError("concern is not current and eligible for revalidation")
        manifest = _source(args).manifest_for(issue)
        if not isinstance(manifest, SourceManifest) or manifest.coverage != "complete":
            raise CommandError(
                f"source evidence cannot be bound ({_unbound_reason(manifest)}); the concern "
                "file and related files must be committed regular files, as repository-relative "
                "paths, under --source-root, the repository top level"
            )
        issue["detail"]["github_repair_revalidated"] = {
            **_record_base(candidate),
            "manifest": manifest.as_record(),
            "manifest_digest": manifest.digest,
        }
    print(f"Revalidated {args.issue_id} for {repository}.")


def _unbound_reason(manifest: Any) -> str:
    if not isinstance(manifest, SourceManifest):
        return manifest.reason
    unbound = [f"{d.path} is {d.status}" for d in manifest.dependencies if d.status != "present"]
    if not any(d.role == "implementation" for d in manifest.dependencies):
        unbound.insert(0, "no concern file")
    return "; ".join(unbound)


def _recover(args: argparse.Namespace, client: Any) -> None:
    _require_apply(args, "recover")
    repository = client.resolve_repository(args.repository)
    cleared = 0
    with _locked_state(args) as state:
        for issue in _issues(state).values():
            detail = issue.get("detail") if isinstance(issue, Mapping) else None
            if not isinstance(detail, dict):
                continue
            if _pending_matches(detail, repository, args.marker):
                detail.pop("github_repair_pending", None)
                cleared += 1
    print(f"Cleared {cleared} pending repair attempt(s).")


def _pending_matches(detail: Mapping[str, Any], repository: str, marker: str) -> bool:
    """Match a pending record by its literal key/legacy marker or by its normalized key."""
    pending = detail.get("github_repair_pending")
    if not isinstance(pending, Mapping) or pending.get("repository") != repository:
        return False
    if marker in (pending.get("key"), pending.get("marker")):
        return True
    hashes = concern_hashes(detail)
    if hashes is None:
        return False
    try:
        record = normalize_record("github_repair_pending", pending, repository, *hashes)
    except RepairRecordError:
        return False
    return record is not None and record["key"] == marker


def _sync(args: argparse.Namespace, client: Any) -> None:
    state = _read_state(args)
    candidates = _candidates(state, args.repository)
    key_counts = Counter(candidate.key for candidate in candidates)
    for candidate in candidates:
        if key_counts[candidate.key] > 1:
            print(f"Skipped {candidate.issue_id}: concern key is ambiguous across work items.")
        else:
            _sync_one(args, client, state, candidate)


def _sync_one(
    args: argparse.Namespace, client: Any, state: Mapping[str, Any], candidate: PromotionCandidate
) -> None:
    if not _source_current(args, state, candidate):
        return
    detail = _issues(state)[candidate.issue_id]["detail"]
    try:
        link = matching_record(detail, "github_repair", candidate)
        pending = matching_record(detail, "github_repair_pending", candidate)
    except RepairRecordError:
        print(
            f"Skipped {candidate.issue_id}: repair record is unrecognized; reconcile it manually."
        )
        return
    if link is not None:
        _read_link(args, client, candidate, link, expected_key="github_repair")
        return
    if pending is None and _resolved_by_peer(args, client, state, candidate):
        return
    found = _search(client, candidate)
    matches = _verified_matches(candidate, found)
    if found is None:
        print(f"Skipped {candidate.issue_id}: GitHub search failed.")
    elif matches is None:
        print(f"Skipped {candidate.issue_id}: GitHub matches do not carry the concern key.")
    elif pending is not None:
        _adopt_if_unique(args, candidate, matches, "github_repair_pending")
    elif len(matches) == 1:
        _adopt_if_unique(args, candidate, matches, None)
    elif len(matches) > 1:
        print(f"Skipped {candidate.issue_id}: concern key is ambiguous on GitHub.")
    elif args.apply:
        _create_once(args, client, candidate)
    else:
        _preview_create(state, candidate)


def _preview_create(state: Mapping[str, Any], candidate: PromotionCandidate) -> None:
    brief = build_brief(_issues(state)[candidate.issue_id], candidate)
    if isinstance(brief, ParkedBrief):
        print(f"Would park {_parked(candidate, brief)}")
    else:
        print(f"Would create a repair issue for {candidate.issue_id}.")


def _parked(candidate: PromotionCandidate, brief: ParkedBrief) -> str:
    """Name the field and fixed category only; a rejected value is never printed."""
    return (
        f"{candidate.issue_id}: brief field {brief.field} cannot be published "
        f"({brief.reason}); hand the concern off privately."
    )


def _source_current(
    args: argparse.Namespace, state: Mapping[str, Any], candidate: PromotionCandidate
) -> bool:
    """Compare without the lock; on a mismatch, clear under the lock (``--apply`` only)."""
    if _comparison(args, _issues(state)[candidate.issue_id]).current:
        return True
    if not args.apply:
        print(f"Skipped {candidate.issue_id}: source evidence is not current.")
        return False
    with _locked_state(args) as locked:
        if _candidate_by_id(locked, candidate.issue_id, candidate.repository) != candidate:
            print(f"Skipped {candidate.issue_id}: result is stale.")
            return False
        return _recheck_locked(args, locked, candidate)


def _recheck_locked(
    args: argparse.Namespace, state: dict[str, Any], candidate: PromotionCandidate
) -> bool:
    """Inside a state-lock transaction: proceed only on current source evidence."""
    issue = _issues(state)[candidate.issue_id]
    comparison = _comparison(args, issue)
    if comparison.current:
        return True
    if comparison.current_digest is None:
        print(f"Skipped {candidate.issue_id}: source could not be read; revalidation kept.")
        return False
    issue["detail"].pop("github_repair_revalidated", None)
    print(f"Skipped {candidate.issue_id}: source evidence is not current ({comparison.reason}).")
    return False


def _comparison(args: argparse.Namespace, issue: Mapping[str, Any]) -> ManifestComparison:
    record = issue["detail"].get("github_repair_revalidated")
    stored = manifest_from_record(record.get("manifest") if isinstance(record, Mapping) else None)
    return compare_manifests(stored, _source(args).manifest_for(issue))


def _source(args: argparse.Namespace) -> Any:
    supplied = getattr(args, "source", None)
    if supplied is not None:
        return supplied
    root = getattr(args, "source_root", None)
    revision = getattr(args, "revision", None) or "HEAD"
    return SourceCheckout(Path(root) if root else get_project_root(), revision)


def _resolved_by_peer(
    args: argparse.Namespace, client: Any, state: Mapping[str, Any], candidate: PromotionCandidate
) -> bool:
    """Park on or adopt another work item's record for this key; False when none exists."""
    peers = _peer_records(state, candidate)
    if any(kind != "github_repair" or record is None for _, kind, record in peers):
        print(
            f"Skipped {candidate.issue_id}: another work item holds an unresolved record "
            "for this concern key."
        )
        return True
    peer_links = {record["number"]: record for _, _, record in peers if record is not None}
    if len(peer_links) > 1:
        print(f"Skipped {candidate.issue_id}: local links for this concern key conflict.")
    elif peer_links:
        _read_link(args, client, candidate, next(iter(peer_links.values())), expected_key=None)
    return bool(peer_links)


def _read_link(
    args: argparse.Namespace,
    client: Any,
    candidate: PromotionCandidate,
    link: Mapping[str, Any],
    *,
    expected_key: str | None,
) -> None:
    try:
        issue = client.view(candidate.repository, link["number"])
    except (RuntimeError, ValueError):
        print(f"Skipped {candidate.issue_id}: linked issue could not be read.")
        return
    if args.apply:
        _write_link(args, candidate, issue, expected_key=expected_key)
    else:
        print(f"Would reconcile linked issue #{issue.number} for {candidate.issue_id}.")


def _search(client: Any, candidate: PromotionCandidate) -> list[Any] | None:
    """Search the stable key, the identity digest, and the current legacy marker."""
    terms = (
        candidate.key,
        candidate.identity,
        legacy_marker(candidate.identity, candidate.evidence_digest),
    )
    try:
        found = [issue for term in terms for issue in client.search(candidate.repository, term)]
    except (RuntimeError, ValueError):
        return None
    return list({issue.number: issue for issue in found}.values())


def _verified_matches(
    candidate: PromotionCandidate, matches: list[Any] | None
) -> list[Any] | None:
    """Keep hits whose body carries the key; ``None`` when only unverified hits exist."""
    if matches is None:
        return None
    verified = [issue for issue in matches if carries_concern_marker(issue.body, candidate)]
    return None if matches and not verified else verified


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
            or _has_record(detail, candidate)
            or _key_claimed_elsewhere(state, candidate)
        ):
            print(f"Skipped {candidate.issue_id}: concern changed while preparing create.")
            return
        if not _recheck_locked(args, state, candidate):
            return
        brief = build_brief(_issues(state)[candidate.issue_id], candidate)
        if isinstance(brief, ParkedBrief):
            print(f"Parked {_parked(candidate, brief)}")
            return
        detail["github_repair_pending"] = _record_base(candidate)
    try:
        client.create(candidate.repository, *render_brief(brief))
    except (RuntimeError, ValueError):
        print(f"Pending {candidate.issue_id}: create outcome is uncertain.")
        return
    found = _search(client, candidate)
    matches = _verified_matches(candidate, found)
    if found is None:
        print(f"Pending {candidate.issue_id}: post-create search failed.")
        return
    if matches is None:
        print(f"Pending {candidate.issue_id}: GitHub matches do not carry the concern key.")
        return
    _adopt_if_unique(args, candidate, matches, "github_repair_pending")


def _write_link(args: argparse.Namespace, candidate: PromotionCandidate, issue: Any, *, expected_key: str | None) -> None:
    with _locked_state(args) as state:
        fresh = _candidate_by_id(state, candidate.issue_id, candidate.repository)
        if fresh != candidate:
            print(f"Skipped {candidate.issue_id}: result is stale.")
            return
        if not _recheck_locked(args, state, candidate):
            return
        detail = _issues(state)[candidate.issue_id]["detail"]
        if expected_key and _expected_record(detail, expected_key, candidate) is None:
            print(f"Skipped {candidate.issue_id}: result is stale.")
            return
        detail["github_repair"] = {
            **_record_base(candidate),
            "number": issue.number,
            "url": issue.url,
            "state": issue.state,
        }
        detail.pop("github_repair_pending", None)
    print(f"Linked {candidate.issue_id} to issue #{issue.number}.")


def _record_base(candidate: PromotionCandidate) -> dict[str, str]:
    return {
        "key": candidate.key,
        "repository": candidate.repository,
        "evidence_digest": candidate.evidence_digest,
    }


def _has_record(detail: Mapping[str, Any], candidate: PromotionCandidate) -> bool:
    try:
        return any(
            matching_record(detail, kind, candidate) is not None
            for kind in ("github_repair", "github_repair_pending")
        )
    except RepairRecordError:
        return True


def _expected_record(
    detail: Mapping[str, Any], kind: str, candidate: PromotionCandidate
) -> Mapping[str, Any] | None:
    try:
        return matching_record(detail, kind, candidate)
    except RepairRecordError:
        return None


def _candidates(state: Mapping[str, Any], repository: str) -> list[PromotionCandidate]:
    found = (candidate_from_issue(issue, repository) for issue in _issues(state).values())
    return [candidate for candidate in found if candidate is not None]


def _peer_records(
    state: Mapping[str, Any], candidate: PromotionCandidate
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Return link/pending records other work items hold for this key; ``None`` if unrecognized."""
    peers: list[tuple[str, str, dict[str, Any] | None]] = []
    for issue_id, issue in _issues(state).items():
        if issue_id == candidate.issue_id or not isinstance(issue, Mapping):
            continue
        detail = issue.get("detail")
        hashes = concern_hashes(detail) if isinstance(detail, Mapping) else None
        if hashes is None or concern_key(candidate.repository, hashes[0]) != candidate.key:
            continue
        for kind in ("github_repair", "github_repair_pending"):
            if detail.get(kind) is None:
                continue
            try:
                record = normalize_record(kind, detail[kind], candidate.repository, *hashes)
            except RepairRecordError:
                record = None
            peers.append((issue_id, kind, record))
    return peers


def _key_claimed_elsewhere(state: Mapping[str, Any], candidate: PromotionCandidate) -> bool:
    holders = [
        peer for peer in _candidates(state, candidate.repository) if peer.key == candidate.key
    ]
    return len(holders) > 1 or bool(_peer_records(state, candidate))


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


__all__ = ["cmd_repair_queue"]
