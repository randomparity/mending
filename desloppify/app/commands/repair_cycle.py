"""One-shot, fail-closed composition of one bounded repair cycle (ADRs 0015, 0016)."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import subprocess  # nosec B404
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar, cast

from desloppify.app.commands.helpers.state import state_path
from desloppify.app.commands.repair_cycle_host import (
    ClaudeHostAdapter,
    HostLookupError,
    HostOutcome,
    HostRequest,
    PullRequest,
    host_session_id,
)
from desloppify.app.commands.repair_queue import cmd_repair_queue, source_comparison
from desloppify.base.discovery.paths import get_project_root
from desloppify.base.exception_sets import CommandError
from desloppify.engine._state.persistence import save_state, state_lock
from desloppify.engine._state.schema import get_state_file
from desloppify.engine._state.schema_types import StateModel
from desloppify.engine.repair_authority import (
    Approval,
    Authority,
    AuthorityBinding,
    UnsupportedAuthority,
    authority_from_mapping,
    check_authority,
)
from desloppify.engine.repair_brief import (
    RepairBrief,
    build_brief,
    render_brief,
    reviewed_version,
)
from desloppify.engine.repair_cycle import (
    CycleConfig,
    CycleLease,
    CycleState,
    DispatchRecord,
    window_start,
)
from desloppify.engine.repair_manifest import SourceManifest, manifest_from_record
from desloppify.engine.repair_queue import (
    PromotionCandidate,
    RepairRecordError,
    candidate_from_issue,
    matching_record,
)
from desloppify.engine.repair_selection import rank_key

_Result = TypeVar("_Result")
# Discovery-only runs: nothing published is selectable, or nothing selectable is approved.
_NO_OP_REASONS = frozenset({"selected-repair-unavailable", "authority-missing"})


def cmd_repair_cycle(args: argparse.Namespace) -> None:
    """Run one bounded cycle: observe, refresh, publish, select, then dispatch at most once."""
    config = _load_config(args)
    with _cycle_lock(args) as held:
        if not held:
            print("Repair cycle already running.")
            return
        _run_cycle(args, config)


def _run_cycle(args: argparse.Namespace, config: CycleConfig) -> None:
    host = cast(ClaudeHostAdapter, getattr(args, "host", None) or ClaudeHostAdapter(config))
    dispose_attempt = getattr(args, "dispose_attempt", None)
    with _locked_state(args) as state:
        cycle_state = _cycle_state(state)
        if dispose_attempt is not None:
            confirm_stopped = getattr(args, "confirm_stopped", False) is True
            _dispose(state, cycle_state, dispose_attempt, confirm_stopped)
            return
        if not cycle_state.settled:
            _observe(args, state, cycle_state, config, host)
            if not cycle_state.settled:
                return
        if _refused_new_work(state, cycle_state, config, _now(args)):
            return
    # scan and sync write the state file themselves, so the state lock is released here;
    # the cycle lock keeps another cycle out meanwhile (ADR 0016).
    refused = _refresh(args, config) or _publish(args, config)
    with _locked_state(args) as state:
        cycle_state = _cycle_state(state)
        if refused is not None:
            _park(state, cycle_state, refused)
            return
        now = _now(args)
        if not _refused_new_work(state, cycle_state, config, now):
            _begin_and_dispatch(args, state, cycle_state, config, host, now)


def _refused_new_work(
    state: dict[str, Any], cycle_state: CycleState, config: CycleConfig, now: datetime
) -> bool:
    """Park and return True when this run may not select new work."""
    lease = cycle_state.current_lease
    if config.park_reason is not None:
        reason: str | None = config.park_reason
    elif cycle_state.awaiting_disposition:
        reason = "disposition-required"
    elif not cycle_state.settled:
        reason = "attempt-active"
    elif lease is not None and lease.window_start == window_start(now, config.window_minutes):
        reason = "window-attempt-complete"
    else:
        reason = None
    if reason is not None:
        _park(state, cycle_state, reason)
    return reason is not None


def _begin_and_dispatch(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    host: ClaudeHostAdapter,
    now: datetime,
) -> None:
    # Authorized before the lease exists, so a refusal leaves no attempt to reconcile.
    try:
        authorized = _call_within(
            config.runtime_seconds,
            lambda: _authorize(args, state, cycle_state, config, now, select=True),
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return
    if isinstance(authorized, str):
        if authorized in _NO_OP_REASONS:
            _no_op(state, cycle_state, authorized)
        else:
            _park(state, cycle_state, authorized)
        return
    lease = cycle_state.begin(config, now)
    if lease is None:
        _park(state, cycle_state, "window-attempt-complete")
        return
    cycle_state.authority = authorized[1]
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    _dispatch_host(args, state, cycle_state, config, host)


def _refresh(args: argparse.Namespace, config: CycleConfig) -> str | None:
    """Rescan the checkout into the cycle's own state file; a park reason on failure."""
    supplied = getattr(args, "refresh", None)
    if callable(supplied):
        return cast("str | None", supplied())
    # -P keeps a checkout package from shadowing the installed desloppify.
    argv = [
        sys.executable, "-P", "-m", "desloppify", "scan", "--no-badge",
        "--state", str(_state_file(args).resolve()),
    ]
    try:
        done = subprocess.run(  # nosec B603
            argv, cwd=_repo_root(args), timeout=config.runtime_seconds, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Repair cycle refresh failed: {exc}")
        return "refresh-failed"
    return None if done.returncode == 0 else "refresh-failed"


def _publish(args: argparse.Namespace, config: CycleConfig) -> str | None:
    """Revalidate and publish through repair-queue sync (ADR 0014); a park reason on failure."""
    sync = argparse.Namespace(
        repair_queue_action="sync",
        repository=config.repository,
        apply=True,
        state=getattr(args, "state", None),
        state_data=getattr(args, "state_data", None),
        source=getattr(args, "source", None),
        check=getattr(args, "check", None),
        client=getattr(args, "queue_client", None),
        source_root=None,
        revision="HEAD",
    )
    try:
        _call_within(config.runtime_seconds, lambda: cmd_repair_queue(sync))
    except (CommandError, RuntimeError, OSError, ValueError, TimeoutError) as exc:
        print(f"Repair cycle publication failed: {exc}")
        return "publication-failed"
    return None


def _observe(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    host: ClaudeHostAdapter,
) -> None:
    """Spend one bounded observation on an unsettled attempt; never start work (#28)."""
    if cycle_state.observation_calls >= config.observation_call_limit:
        _fail(state, cycle_state, "observation-exhausted")
        return
    cycle_state.observation_calls += 1
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    try:
        _call_within(
            config.observation_seconds,
            lambda: _observe_once(args, state, cycle_state, config, host),
        )
    except TimeoutError:
        _park(state, cycle_state, "observation-timeout")


def _observe_once(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    host: ClaudeHostAdapter,
) -> None:
    record = cycle_state.dispatch
    repo_root = _repo_root(args)
    if record is None:
        # No dispatch record, yet unsettled: a legacy active receipt or a held reservation.
        reserved = cycle_state.reserved_calls or cycle_state.reserved_cost_usd
        _fail(state, cycle_state, "unsettled-reservation" if reserved else "dispatch-outcome-unknown")
        return
    if record.phase != "returned" or record.outcome == "unknown":
        if _replay_dispatch(state, cycle_state, host, repo_root):
            prs = _read_pull_requests(state, cycle_state, host)
            scope = _scope_failure(args, config, cycle_state, prs) if prs is not None else None
            if scope is not None:
                _fail(state, cycle_state, scope)
        return
    if not _add_references(state, cycle_state, host, repo_root):
        _park(state, cycle_state, "pull-request-lookup-unavailable")
        return
    _check_pull_requests(args, state, cycle_state, config, host)


def _check_pull_requests(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    host: ClaudeHostAdapter,
) -> None:
    """Settle a returned dispatch by its pull requests (ADR 0016)."""
    record = cycle_state.dispatch
    prs = _read_pull_requests(state, cycle_state, host)
    if record is None or prs is None:
        return
    cycle_state.observation_calls = 0  # a verdict was reached; the allowance bounds misses
    scope = _scope_failure(args, config, cycle_state, prs)
    if scope == "authority-scope-exceeded":
        _fail(state, cycle_state, scope)
    elif not prs and record.outcome == "completed":
        _fail(state, cycle_state, "no-pull-request")
    elif any(pr.open for pr in prs):
        # The resume recheck names a missing or changed approval more precisely.
        print("Repair cycle active: pull request open.")
        _store_cycle_state(state, cycle_state)
        _recheck_active(args, state, cycle_state, config)
    elif scope is not None:
        _fail(state, cycle_state, scope)
    else:
        cycle_state.dispatch = replace(record, phase="settled")
        _store_cycle_state(state, cycle_state)
        print("Repair cycle settled.")


def _read_pull_requests(
    state: dict[str, Any], cycle_state: CycleState, host: ClaudeHostAdapter
) -> tuple[PullRequest, ...] | None:
    record = cycle_state.dispatch
    try:
        return host.pull_requests(record.pull_requests if record else ())
    except HostLookupError as exc:
        print(f"Repair cycle lookup failed: {exc}")
        _park(state, cycle_state, "pull-request-lookup-unavailable")
        return None


def _scope_failure(
    args: argparse.Namespace,
    config: CycleConfig,
    cycle_state: CycleState,
    prs: tuple[PullRequest, ...],
) -> str | None:
    """Why the pull requests' edits are not within the bound approval (#26), if they are not.

    Edits that cannot be compared, because the approval is gone or unreadable,
    fail closed rather than settle.
    """
    if not prs:
        return None
    approval = _bound_approval(args, config, cycle_state.authority)
    if isinstance(approval, str):
        return approval
    return "authority-scope-exceeded" if any(not pr.files <= approval.files for pr in prs) else None


def _bound_approval(
    args: argparse.Namespace, config: CycleConfig, bound: Mapping[str, str] | None
) -> Approval | str:
    """The trusted configuration's approval for the bound key, or why there is none."""
    authority = _trusted_authority(args, config)
    if isinstance(authority, str):
        return authority
    if bound is None:
        return "authority-missing"
    approvals = authority.approvals if authority else ()
    return next((a for a in approvals if a.key == bound["key"]), None) or "authority-revoked"


def _recheck_active(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
) -> None:
    """On resume, an active attempt whose authority no longer holds fails (ADR 0015).

    It runs inside the observation's time bound.
    """
    authorized = _authorize(args, state, cycle_state, config, _now(args))
    if authorized == "source-unreadable":
        _park(state, cycle_state, authorized)  # transient: the next observation rechecks
    elif isinstance(authorized, str):
        _fail(state, cycle_state, authorized)


def _authorize(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    now: datetime,
    *,
    select: bool = False,
) -> tuple[AuthorityBinding, dict[str, str]] | str:
    """The one authority check (ADR 0015): the binding and its record, else a park reason.

    Selection (``select``) picks the work item; every later check rebuilds the
    binding from the work item the attempt was bound to.
    """
    bound = None if select else cycle_state.authority
    if not select and bound is None:
        return "authority-missing"
    authority = _trusted_authority(args, config)
    if isinstance(authority, str):
        return authority
    issue_id = bound["issue_id"] if bound else _selected_issue(state, config.repository, authority)
    item = _work_item(state, issue_id, config.repository)
    if issue_id is None or item is None:
        return "selected-repair-unavailable"
    issue, candidate, version = item
    if bound and bound["key"] != candidate.key:
        return "authority-mismatch"
    recheck = source_comparison(args, issue)
    if not recheck.current:
        return "source-unreadable" if recheck.keep else "source-not-current"
    limits = _limits(None if select else cycle_state.current_lease, config, now)
    if limits is None:
        return "missing-cost-cap"
    binding = _binding(issue, candidate, version, limits)
    reason = check_authority(authority, binding, now, bound is not None)
    if reason is not None or authority is None:
        return reason or "authority-missing"
    return binding, {"issue_id": issue_id, "key": candidate.key, "revision": authority.revision}


def _work_item(
    state: Mapping[str, object], issue_id: str | None, repository: str
) -> tuple[Mapping[str, Any], PromotionCandidate, str] | None:
    """The work item as a small repair with its current reviewed-brief version."""
    items = state.get("work_items")
    issue = items.get(issue_id) if isinstance(items, Mapping) and issue_id else None
    if not isinstance(issue, Mapping):
        return None
    candidate = candidate_from_issue(issue, repository)
    version = reviewed_version(issue, candidate) if candidate else None
    if candidate is None or version is None:
        return None
    return issue, candidate, version


def _trusted_authority(args: argparse.Namespace, config: CycleConfig) -> Authority | None | str:
    """Decode the configuration's authority, or name why it cannot grant any."""
    if not _trusted_config(args):
        return "authority-untrusted"
    try:
        return authority_from_mapping(config.repository, config.authority)
    except UnsupportedAuthority:
        return "authority-unsupported"
    except ValueError:
        return "authority-invalid"


def _limits(
    lease: CycleLease | None, config: CycleConfig, now: datetime
) -> tuple[int, Decimal, int] | None:
    """Calls, USD cap, and seconds the attempt may use: the lease's once it exists."""
    if lease is not None:
        remaining = max(0, math.ceil((lease.deadline - now).total_seconds()))
        return lease.call_limit, lease.cost_cap_usd, remaining
    if config.cost_cap_usd is None:
        return None
    return config.call_limit, config.cost_cap_usd, config.runtime_seconds


def _binding(
    issue: Mapping[str, Any],
    candidate: PromotionCandidate,
    version: str,
    limits: tuple[int, Decimal, int],
) -> AuthorityBinding:
    record = matching_record(issue["detail"], "github_repair_revalidated", candidate) or {}
    manifest = manifest_from_record(record.get("manifest"))
    files = manifest.dependencies if isinstance(manifest, SourceManifest) else ()
    return AuthorityBinding(
        repository=candidate.repository,
        key=candidate.key,
        reviewed_brief_version=version,
        evidence_digest=candidate.evidence_digest,
        files=frozenset(dependency.path for dependency in files),
        action="repair",
        call_limit=limits[0],
        cost_cap_usd=limits[1],
        runtime_seconds=limits[2],
    )


def _selected_issue(
    state: Mapping[str, object], repository: str, authority: Authority | None
) -> str | None:
    """The published repair to bind: an approved one first, then ADR 0014's rank order."""
    approved = {approval.key for approval in authority.approvals} if authority else set()
    items = state.get("work_items")
    choices = []
    for issue_id, issue in items.items() if isinstance(items, Mapping) else ():
        candidate = candidate_from_issue(issue, repository) if isinstance(issue, Mapping) else None
        if candidate is None:
            continue
        try:
            link = matching_record(issue["detail"], "github_repair", candidate)
        except RepairRecordError:
            continue
        if link is not None and link.get("state") != "closed":
            choices.append((candidate.key not in approved, rank_key(issue, candidate), issue_id))
    return min(choices)[2] if choices else None


def _trusted_config(args: argparse.Namespace) -> bool:
    """Only a configuration this account neither owns nor can write grants authority.

    The coding host runs as this account (ADR 0010), so an owned file could be
    made writable again; every ancestor directory is checked because a writable
    one lets the file be replaced. A symlinked path is refused, so the checked
    file is the one that was read.
    """
    if isinstance(getattr(args, "config_data", None), Mapping):
        return True
    named = Path(str(getattr(args, "config", ""))).absolute()
    path = named.resolve()
    if path != named:  # a symlink or ".." component: the checked file may not be the read one
        return False
    account = os.geteuid()
    for entry in (path, *path.parents):
        try:
            owner = entry.stat().st_uid
        except OSError:
            return False
        if owner == account or os.access(entry, os.W_OK):
            return False
    return True


def _dispatch_host(
    args: argparse.Namespace,
    state: dict[str, Any],
    cycle_state: CycleState,
    config: CycleConfig,
    adapter: ClaudeHostAdapter,
) -> HostOutcome | None:
    """Recheck, admit, persist, run, and settle one host dispatch under the held state lock.

    The authority check here is the execution-time source and authority recheck,
    and the request is built from the binding it returns. An attempt that already
    has a dispatch record is replayed, never launched again. Returns None when
    nothing was dispatched.
    """
    lease = cycle_state.current_lease
    if lease is None:
        raise ValueError("no current lease to dispatch")
    repo_root = _repo_root(args)
    if cycle_state.dispatch is not None:
        _replay_dispatch(state, cycle_state, adapter, repo_root)
        return None
    if cycle_state.reserved_calls or cycle_state.reserved_cost_usd:
        # An interrupted dispatch may still be spending; that outranks every other refusal.
        _fail(state, cycle_state, "unsettled-reservation")
        return None
    if _now(args) >= lease.deadline:
        _fail(state, cycle_state, "runtime-exhausted")
        return None
    try:
        authorized = _call_before_deadline(
            args, lease, lambda: _authorize(args, state, cycle_state, config, _now(args))
        )
    except TimeoutError:
        _park(state, cycle_state, "timeout")
        return None
    if isinstance(authorized, str):
        _park(state, cycle_state, authorized)
        return None
    request = _host_request(args, state, config, lease, repo_root, *authorized)
    if request is None:
        _park(state, cycle_state, "selected-repair-unavailable")
        return None
    try:
        prior_worktrees = adapter.worktrees(repo_root)
    except HostLookupError as exc:
        print(f"Repair cycle lookup failed: {exc}")
        _park(state, cycle_state, "dispatch-lookup-unavailable")
        return None
    admission = cycle_state.admit()
    if admission is None:
        _fail(state, cycle_state, "budget-exhausted")
        return None
    record = DispatchRecord(
        lease.attempt_id,
        "intent",
        host_session_id(lease.attempt_id),
        adapter.repository,
        str(repo_root.resolve()),
        prior_worktrees,
    )
    cycle_state.dispatch = record
    _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    outcome = adapter.run(request, admission)
    if outcome.state == "parked":
        cycle_state.dispatch = None
        cycle_state.settle(admission, Decimal(0), outcome.calls)
        _park(state, cycle_state, outcome.reason or "host-parked")
        return outcome
    cycle_state.dispatch = replace(record, phase="returned", outcome=outcome.state)
    if outcome.state in {"stopped", "unknown"}:
        # Work in flight or still running when Mending stopped it may have spent anything.
        cycle_state.settle(admission, None, max(outcome.calls, admission.calls))
    else:
        cycle_state.settle(admission, outcome.cost_usd, outcome.calls)
    failure = "budget-exhausted" if cycle_state.budget_exceeded else _outcome_failure(outcome)
    if failure is not None:
        _fail(state, cycle_state, failure)
    else:
        _store_cycle_state(state, cycle_state)
    _persist_before_external_call(args, state)
    found = _add_references(state, cycle_state, adapter, repo_root)
    if outcome.state == "completed" and failure is None:
        if found:
            _check_pull_requests(args, state, cycle_state, config, adapter)
        else:
            _park(state, cycle_state, "pull-request-lookup-unavailable")
    return outcome


def _outcome_failure(outcome: HostOutcome) -> str | None:
    """A host run that did not complete stops new repairs until a disposition."""
    if outcome.state == "failed":
        return "host-failed"
    if outcome.state == "stopped":
        return "runtime-exhausted" if outcome.reason == "timeout" else "budget-exhausted"
    if outcome.state == "unknown":
        return "dispatch-outcome-unknown"
    return None


def _host_request(
    args: argparse.Namespace,
    state: Mapping[str, object],
    config: CycleConfig,
    lease: CycleLease,
    repo_root: Path,
    binding: AuthorityBinding,
    bound: Mapping[str, str],
) -> HostRequest | None:
    """The reviewed brief and approved scope for the binding the dispatch check returned."""
    item = _work_item(state, bound["issue_id"], config.repository)
    brief = build_brief(item[0], item[1]) if item else None
    approval = _bound_approval(args, config, bound)
    if not isinstance(brief, RepairBrief) or isinstance(approval, str):
        return None
    return HostRequest(
        attempt_id=lease.attempt_id,
        brief=render_brief(brief)[1],
        source_revision=brief.revision,
        authorized_scope=f"{binding.action} {binding.key}; files: {', '.join(sorted(approval.files))}",
        repo_root=repo_root,
        deadline=lease.deadline,
    )


def _replay_dispatch(
    state: dict[str, Any],
    cycle_state: CycleState,
    adapter: ClaudeHostAdapter,
    repo_root: Path,
) -> bool:
    """Observe what a recorded dispatch left behind; never launch it again.

    True when the worker is verified stopped and its references were read.
    """
    record = cycle_state.dispatch
    alive = adapter.worker_alive(record.attempt_id) if record else None
    if alive is not False:
        _park(state, cycle_state, "dispatch-in-flight" if alive else "dispatch-unverified")
        return False
    found = _add_references(state, cycle_state, adapter, repo_root)
    if cycle_state.reserved_calls or cycle_state.reserved_cost_usd:
        _fail(state, cycle_state, "unsettled-reservation")
    elif record is not None and (record.phase == "unknown" or record.outcome == "unknown"):
        _fail(state, cycle_state, "dispatch-outcome-unknown")
    elif not found:
        _park(state, cycle_state, "dispatch-lookup-unavailable")
    else:
        _park(state, cycle_state, "already-dispatched")
    return found


def _add_references(
    state: dict[str, Any],
    cycle_state: CycleState,
    adapter: ClaudeHostAdapter,
    repo_root: Path,
) -> bool:
    """Add what the adapter finds for the recorded dispatch; False when the lookup failed."""
    record = cycle_state.dispatch
    if record is None:
        return False
    try:
        found = adapter.references(
            record.attempt_id,
            record.repository or adapter.repository,
            Path(record.repo_root) if record.repo_root else repo_root,
            record.prior_worktrees,
        )
    except HostLookupError as exc:
        print(f"Repair cycle lookup failed: {exc}")
        return False
    # An unknown record has no pre-launch snapshot, so no worktree can be attributed to it.
    worktrees = () if record.phase == "unknown" else found.worktrees
    cycle_state.dispatch = record.with_references(found.pull_requests, found.issues, worktrees)
    _store_cycle_state(state, cycle_state)
    return True


def _repo_root(args: argparse.Namespace) -> Path:
    """The checkout the cycle scans, rechecks, and hands to the host."""
    supplied = getattr(args, "repo_root", None)
    return Path(supplied) if supplied else get_project_root()


def _call_before_deadline(
    args: argparse.Namespace,
    lease: CycleLease,
    operation: Callable[[], _Result],
) -> _Result:
    remaining_seconds = (lease.deadline - _now(args)).total_seconds()
    if remaining_seconds <= 0:
        raise TimeoutError("repair-cycle runtime exhausted")
    return _call_within(remaining_seconds, operation)


def _call_within(seconds: float, operation: Callable[[], _Result]) -> _Result:
    if threading.current_thread() is not threading.main_thread():
        return operation()
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _deadline_exceeded)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def _deadline_exceeded(_signum: int, _frame: object) -> None:
    raise TimeoutError("repair-cycle call bound exceeded")


def _load_config(args: argparse.Namespace) -> CycleConfig:
    supplied = getattr(args, "config_data", None)
    if isinstance(supplied, Mapping):
        raw = supplied
    else:
        path = getattr(args, "config", None)
        if not isinstance(path, str) or not path:
            raise CommandError("repair-cycle requires --config", exit_code=2)
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError("repair-cycle configuration is unreadable", exit_code=2) from exc
    if not isinstance(raw, Mapping):
        raise CommandError("repair-cycle configuration must be an object", exit_code=2)
    try:
        return CycleConfig.from_mapping(raw)
    except ValueError as exc:
        raise CommandError(f"repair-cycle configuration is invalid: {exc}", exit_code=2) from exc


def _now(args: argparse.Namespace) -> datetime:
    clock = getattr(args, "clock", None)
    supplied = clock() if callable(clock) else getattr(args, "now", None)
    now = supplied if isinstance(supplied, datetime) else datetime.now().astimezone()
    if now.tzinfo is None:
        raise CommandError("repair-cycle requires host-local timezone-aware time", exit_code=2)
    return now


def _cycle_state(state: Mapping[str, object]) -> CycleState:
    raw = state.get("repair_cycle")
    if raw is not None and not isinstance(raw, Mapping):
        raise CommandError("repair-cycle state is invalid")
    try:
        return CycleState.from_mapping(raw)
    except ValueError as exc:
        raise CommandError(f"repair-cycle state is invalid: {exc}") from exc


def _dispose(
    state: dict[str, Any],
    cycle_state: CycleState,
    attempt_id: str,
    confirm_stopped: bool,
) -> None:
    try:
        cycle_state.dispose(attempt_id, confirm_stopped=confirm_stopped)
    except ValueError as exc:
        raise CommandError(f"repair-cycle cannot dispose attempt: {exc}", exit_code=2) from exc
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle attempt {attempt_id} disposed.")


def _no_op(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.parked_reason = None
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle no-op: {reason}.")


def _park(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.park(reason)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle parked: {reason}.")


def _fail(state: dict[str, Any], cycle_state: CycleState, reason: str) -> None:
    cycle_state.fail(reason)
    _store_cycle_state(state, cycle_state)
    print(f"Repair cycle parked: {reason}.")


def _store_cycle_state(state: dict[str, Any], cycle_state: CycleState) -> None:
    state["repair_cycle"] = cycle_state.to_mapping()


def _persist_before_external_call(args: argparse.Namespace, state: dict[str, Any]) -> None:
    if isinstance(getattr(args, "state_data", None), dict):
        return
    save_state(cast(StateModel, state), state_path(args))


def _state_file(args: argparse.Namespace) -> Path:
    """The state file every step of the cycle reads and writes."""
    return state_path(args) or get_state_file()


@contextmanager
def _cycle_lock(args: argparse.Namespace) -> Iterator[bool]:
    """Hold one cycle per state file across the unlocked refresh; False when another runs."""
    if isinstance(getattr(args, "state_data", None), dict):
        yield True
        return
    lock_path = _state_file(args).with_suffix(".json.cycle.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(descriptor)


@contextmanager
def _locked_state(args: argparse.Namespace) -> Iterator[dict[str, Any]]:
    supplied = getattr(args, "state_data", None)
    if isinstance(supplied, dict):
        yield supplied
        return
    with state_lock(state_path(args)) as state:
        yield cast(dict[str, Any], state)


__all__ = ["cmd_repair_cycle"]
