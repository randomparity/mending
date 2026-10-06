"""One-shot, fail-closed promotion of revalidated repair candidates and proposals."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from desloppify.app.commands.helpers.state import state_path
from desloppify.base.discovery.paths import get_project_root
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import load_state, state_lock
from desloppify.engine.repair_brief import (
    ParkedBrief,
    build_brief,
    render_brief,
    reviewed_version,
)
from desloppify.engine.repair_check import CheckResult, check_concern, check_finding
from desloppify.engine.repair_manifest import (
    SourceCheckout,
    SourceManifest,
    compare_manifests,
    manifest_from_record,
)
from desloppify.engine.repair_queue import (
    LINK_KINDS,
    GitHubIssueClient,
    PromotionCandidate,
    RepairRecordError,
    carries_concern_marker,
    classify,
    item_hashes,
    lane_for,
    legacy_marker,
    matching_record,
    normalize_record,
    proposal_marker,
    record_key,
)
from desloppify.engine.repair_selection import rank_key


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
    refused: CheckResult | None = None
    with _locked_state(args) as state:
        issue = _issues(state).get(args.issue_id)
        classification = classify(issue or {}, repository, require_revalidation=False)
        candidate = classification.candidate
        if candidate is None:
            raise CommandError(
                f"work item is not eligible for revalidation ({classification.reason})"
            )
        manifest = _source(args).manifest_for(issue)
        if not isinstance(manifest, SourceManifest) or manifest.coverage != "complete":
            raise CommandError(
                f"source evidence cannot be bound ({_unbound_reason(manifest)}); the concern "
                "file and related files must be committed regular files, as repository-relative "
                "paths, under --source-root, the repository top level"
            )
        check = _check(args, issue, manifest)
        if check.outcome != "pass":
            refused = check
            issue["detail"].pop("github_repair_revalidated", None)
        else:
            issue["detail"]["github_repair_revalidated"] = {
                **_record_base(candidate),
                "manifest": manifest.as_record(),
                "manifest_digest": manifest.digest,
                "check": check.as_record(),
                "check_digest": check.digest,
            }
    if refused is not None:
        raise CommandError(
            f"verification check did not pass ({refused.outcome}: {refused.reason}); a "
            "concern's PATH:LINE citations and quoted identifiers, or a duplicate pair's "
            "line ranges, names, and bodies, must still hold in the recorded files"
        )
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
            for kind in _PENDING_KINDS:
                if _pending_matches(issue, kind, repository, args.marker):
                    detail.pop(kind, None)
                    cleared += 1
    print(f"Cleared {cleared} pending repair attempt(s).")


_PENDING_KINDS = ("github_repair_pending", "github_proposal_pending")


def _pending_matches(issue: Mapping[str, Any], kind: str, repository: str, marker: str) -> bool:
    """Match a pending record by its literal key/legacy marker or by its normalized key."""
    pending = issue["detail"].get(kind)
    if not isinstance(pending, Mapping) or pending.get("repository") != repository:
        return False
    if marker in (pending.get("key"), pending.get("marker")):
        return True
    hashes = item_hashes(issue)
    if hashes is None:
        return False
    route, identity, evidence = hashes
    try:
        record = normalize_record(kind, pending, repository, identity, evidence, route=route)
    except RepairRecordError:
        return False
    if record is None:
        return False
    if kind == "github_proposal_pending":
        return marker in {record["key"], proposal_marker(record["key"])}
    return bool(record["key"] == marker)


def _sync(args: argparse.Namespace, client: Any) -> None:
    """Reconcile every candidate, then publish at most one ranked small repair (ADR 0014)."""
    state = _read_state(args)
    candidates = _candidates(state, args.repository)
    key_counts = Counter(candidate.key for candidate in candidates)
    selectable = []
    for candidate in candidates:
        if key_counts[candidate.key] > 1:
            print(f"Skipped {candidate.issue_id}: concern key is ambiguous across work items.")
        elif _sync_one(args, client, state, candidate):
            selectable.append(candidate)
    _select(args, client, selectable)


def _sync_one(
    args: argparse.Namespace, client: Any, state: Mapping[str, Any], candidate: PromotionCandidate
) -> bool:
    """Reconcile one candidate; True when it is a small repair with nothing to reconcile."""
    if not _source_current(args, state, candidate):
        return False
    detail = _issues(state)[candidate.issue_id]["detail"]
    lane = lane_for(candidate)
    other = [kind for kind in LINK_KINDS if kind not in (lane.link, lane.pending)]
    if any(detail.get(kind) is not None for kind in other):
        print(
            f"Skipped {candidate.issue_id}: it holds a record from its other classification;"
            " a human reconciles it."
        )
        return False
    try:
        link = matching_record(detail, lane.link, candidate)
        pending = matching_record(detail, lane.pending, candidate)
    except RepairRecordError:
        print(
            f"Skipped {candidate.issue_id}: repair record is unrecognized; reconcile it manually."
        )
        return False
    if link is not None:
        _read_link(args, client, candidate, link, expected_key=lane.link)
        return False
    if pending is None and _resolved_by_peer(args, client, state, candidate):
        return False
    found = _search(client, candidate)
    matches = _verified_matches(candidate, found)
    if found is None:
        print(f"Skipped {candidate.issue_id}: GitHub search failed.")
    elif matches is None:
        print(f"Skipped {candidate.issue_id}: GitHub matches do not carry the concern key.")
    elif pending is not None:
        _adopt_if_unique(args, candidate, matches, lane.pending)
    elif matches and candidate.route == "finding":
        print(
            f"Skipped {candidate.issue_id}: a GitHub issue carries this finding key, which"
            " anyone can compute from public source; a human reconciles it."
        )
    elif len(matches) == 1:
        _adopt_if_unique(args, candidate, matches, None)
    elif len(matches) > 1:
        print(f"Skipped {candidate.issue_id}: concern key is ambiguous on GitHub.")
    elif candidate.kind != "proposal":
        return True
    elif args.apply:
        _create_once(args, client, candidate)
    else:
        _preview_proposal(state, candidate)
    return False


def _preview_proposal(state: Mapping[str, Any], candidate: PromotionCandidate) -> None:
    brief = build_brief(_issues(state)[candidate.issue_id], candidate)
    if isinstance(brief, ParkedBrief):
        print(f"Would park {_parked(candidate, brief)}")
    else:
        print(f"Would publish a non-dispatchable proposal issue for {candidate.issue_id}.")


def _select(
    args: argparse.Namespace, client: Any, selectable: list[PromotionCandidate]
) -> None:
    """Create at most one ranked small repair, or record why none was (ADR 0014)."""
    state = _read_state(args)
    blockers = _pending_repairs(state, args.repository)
    for issue_id, pending in blockers:
        if isinstance(pending, Mapping):
            print(
                f"No repair selected: repair publication for {issue_id} is unresolved; check"
                f" GitHub, then run repair-queue recover {pending.get('key', pending.get('marker'))}."
            )
        else:
            print(
                f"No repair selected: the pending repair record on {issue_id} is unrecognized;"
                " reconcile it manually."
            )
    if blockers:
        _record_selection(args, "no-op", "unresolved repair publication", None)
        return
    safe = [candidate for candidate in selectable if _safe(args, state, candidate)]
    if not safe:
        print("No repair selected: no safe small repair.")
        _record_selection(args, "no-op", "no safe small repair", None)
        return
    chosen, *others = sorted(
        safe, key=lambda candidate: rank_key(_issues(state)[candidate.issue_id], candidate)
    )
    for other in others:
        print(f"Deferred {other.issue_id}: {chosen.issue_id} ranked first.")
    if not args.apply:
        print(f"Would create a repair issue for {chosen.issue_id}.")
    elif _create_once(args, client, chosen):
        _record_selection(args, "selected", "ranked first", chosen.issue_id)
    else:
        _record_selection(args, "no-op", "selected candidate changed before create", None)


def _safe(args: argparse.Namespace, state: Mapping[str, Any], candidate: PromotionCandidate) -> bool:
    """A selectable candidate is still current and its brief can be published."""
    if _candidate_by_id(state, candidate.issue_id, candidate.repository) != candidate:
        print(f"Skipped {candidate.issue_id}: result is stale.")
        return False
    brief = build_brief(_issues(state)[candidate.issue_id], candidate)
    if isinstance(brief, ParkedBrief):
        print(f"{'Parked' if args.apply else 'Would park'} {_parked(candidate, brief)}")
        return False
    return True


def _pending_repairs(state: Mapping[str, Any], repository: str) -> list[tuple[str, Any]]:
    """Every work item's repair pending record for ``repository``, or one that is unrecognized."""
    found = []
    for issue_id, issue in _issues(state).items():
        detail = issue.get("detail") if isinstance(issue, Mapping) else None
        pending = detail.get("github_repair_pending") if isinstance(detail, Mapping) else None
        if pending is not None and (
            not isinstance(pending, Mapping) or pending.get("repository") == repository
        ):
            found.append((issue_id, pending))
    return found


def _record_selection(
    args: argparse.Namespace, outcome: str, reason: str, issue_id: str | None
) -> None:
    if not args.apply:
        return
    with _locked_state(args) as state:
        records = state.get("repair_queue_selection")
        if not isinstance(records, dict):
            records = state["repair_queue_selection"] = {}
        records[args.repository] = {
            "outcome": outcome,
            "reason": reason,
            "issue_id": issue_id,
            "at": datetime.now(UTC).isoformat(),
        }


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
    recheck = _comparison(args, issue)
    if recheck.current:
        return True
    if recheck.keep:
        print(
            f"Skipped {candidate.issue_id}: source or its check could not be read "
            f"({recheck.reason}); revalidation kept."
        )
        return False
    issue["detail"].pop("github_repair_revalidated", None)
    print(f"Skipped {candidate.issue_id}: source evidence is not current ({recheck.reason}).")
    return False


@dataclass(frozen=True)
class _Recheck:
    """Whether stored evidence is current; ``keep`` means unreadable, not changed."""

    current: bool
    reason: str
    keep: bool


_CHECK_REASONS = {"fail": "check-failed", "unknown": "check-unknown"}


def _comparison(args: argparse.Namespace, issue: Mapping[str, Any]) -> _Recheck:
    """Compare the stored manifest, then re-run the check on the rebuilt one."""
    record = issue["detail"].get("github_repair_revalidated")
    record = record if isinstance(record, Mapping) else {}
    rebuilt = _source(args).manifest_for(issue)
    comparison = compare_manifests(manifest_from_record(record.get("manifest")), rebuilt)
    if not comparison.current or not isinstance(rebuilt, SourceManifest):
        return _Recheck(False, comparison.reason, comparison.current_digest is None)
    check = _check(args, issue, rebuilt)
    if check.transient:
        return _Recheck(False, check.reason, True)
    if check.digest != record.get("check_digest"):
        return _Recheck(False, _CHECK_REASONS.get(check.outcome, "check-changed"), False)
    return _Recheck(True, "unchanged", False)


def _check(
    args: argparse.Namespace, issue: Mapping[str, Any], manifest: SourceManifest
) -> CheckResult:
    """Run the evidence check; tests inject results through ``args.check``."""
    supplied = getattr(args, "check", None)
    if supplied is not None:
        result: CheckResult = supplied(issue, manifest)
        return result
    check = check_concern if issue.get("detector") == "concerns" else check_finding
    return check(_source(args).root, manifest, issue)


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
    if any(kind != lane_for(candidate).link or record is None for _, kind, record in peers):
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
    """Search the key, the identity digest, a proposal's marker, and a concern's legacy marker."""
    terms = [candidate.key, candidate.identity]
    if candidate.kind == "proposal":
        terms.insert(0, proposal_marker(candidate.key))
    elif candidate.route == "concern":
        terms.append(legacy_marker(candidate.identity, candidate.evidence_digest))
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


def _create_once(args: argparse.Namespace, client: Any, candidate: PromotionCandidate) -> bool:
    """Create one issue for ``candidate``; True once its pending record is written."""
    with _locked_state(args) as state:
        fresh = _candidate_by_id(state, candidate.issue_id, candidate.repository)
        detail = _issues(state)[candidate.issue_id]["detail"]
        if (
            fresh != candidate
            or _has_record(detail, candidate)
            or _key_claimed_elsewhere(state, candidate)
        ):
            print(f"Skipped {candidate.issue_id}: concern changed while preparing create.")
            return False
        if candidate.kind == "small_repair" and _pending_repairs(state, candidate.repository):
            print(f"Skipped {candidate.issue_id}: another repair publication is pending.")
            return False
        if not _recheck_locked(args, state, candidate):
            return False
        brief = build_brief(_issues(state)[candidate.issue_id], candidate)
        if isinstance(brief, ParkedBrief):
            print(f"Parked {_parked(candidate, brief)}")
            return False
        lane = lane_for(candidate)
        detail[lane.pending] = _record_base(candidate)
    try:
        client.create(candidate.repository, *render_brief(brief), ready=lane.ready)
    except (RuntimeError, ValueError):
        print(f"Pending {candidate.issue_id}: create outcome is uncertain.")
        return True
    found = _search(client, candidate)
    matches = _verified_matches(candidate, found)
    if found is None:
        print(f"Pending {candidate.issue_id}: post-create search failed.")
    elif matches is None:
        print(f"Pending {candidate.issue_id}: GitHub matches do not carry the concern key.")
    else:
        _adopt_if_unique(args, candidate, matches, lane.pending)
    return True


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
        lane = lane_for(candidate)
        previous = _expected_record(detail, lane.link, candidate)
        version = reviewed_version(_issues(state)[candidate.issue_id], candidate)
        record = {
            **_record_base(candidate),
            "number": issue.number,
            "url": issue.url,
            "state": issue.state,
        }
        if version is not None:
            record["reviewed_brief_version"] = version
        detail[lane.link] = record
        detail.pop(lane.pending, None)
    if previous is not None and (
        previous["evidence_digest"] != candidate.evidence_digest
        or previous.get("reviewed_brief_version", version) != version
    ):
        print(
            f"Changed {candidate.issue_id}: issue #{issue.number} evidence or reviewed brief"
            " changed materially; an approval bound to the earlier version no longer applies."
        )
    else:
        print(f"Linked {candidate.issue_id} to issue #{issue.number}.")


def _record_base(candidate: PromotionCandidate) -> dict[str, str]:
    return {
        "key": candidate.key,
        "repository": candidate.repository,
        "evidence_digest": candidate.evidence_digest,
    }


def _has_record(detail: Mapping[str, Any], candidate: PromotionCandidate) -> bool:
    try:
        return any(matching_record(detail, kind, candidate) is not None for kind in LINK_KINDS)
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
    found = (classify(issue, repository).candidate for issue in _issues(state).values())
    return [candidate for candidate in found if candidate is not None]


def _peer_records(
    state: Mapping[str, Any], candidate: PromotionCandidate
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """Return link/pending records other work items hold for this key; ``None`` if unrecognized."""
    peers: list[tuple[str, str, dict[str, Any] | None]] = []
    for issue_id, issue in _issues(state).items():
        if issue_id == candidate.issue_id or not isinstance(issue, Mapping):
            continue
        hashes = item_hashes(issue)
        if hashes is None:
            continue
        route, identity, evidence = hashes
        if record_key(route, candidate.repository, identity) != candidate.key:
            continue
        detail = issue["detail"]
        for kind in LINK_KINDS:
            if detail.get(kind) is None:
                continue
            try:
                record = normalize_record(
                    kind, detail[kind], candidate.repository, identity, evidence, route=route
                )
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
    return classify(issue or {}, repository).candidate


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
