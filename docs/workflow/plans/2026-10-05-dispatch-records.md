# Repair-cycle dispatch records plan

Goal: persist a dispatch record (intent, then host references) for each host
dispatch, never relaunch an attempt that has one, and keep replaced attempts in
history. Spec: `docs/workflow/specs/2026-10-05-dispatch-records-design.md`.

Architecture: `DispatchRecord` and `attempt_history` live on `CycleState` in
the engine; `ClaudeHostAdapter` gains two read-only lookups; `_dispatch_host`
persists the intent with the reservation and takes a replay path when a record
exists.

Tech stack: Python 3.11+, pytest, `uv` via the Makefile.

Expected implementation size: 620–680 changed lines (M) — three tasks: engine ~145, adapter ~110, seam ~80, tests ~320.

Estimate revised after the build: the first estimate (330–420) predated the
design review, whose accepted fixes added the pre-launch worktree snapshot, the
recorded repository/root, reference union, and persist-before-lookup ordering,
and it undercounted the ten-field record's decoding and its tests. Scope is
unchanged.

## Global Constraints

- No new dependency. `gh` and `git` are invoked with fixed argv, no shell,
  `timeout=10`.
- The engine (`desloppify/engine`) must not import from `desloppify/app`
  (`make arch`).
- Existing persisted state must decode; legacy leases are never discarded.
- Guardrails, each must pass: `make lint`, `make typecheck`, `make arch`,
  `make ci-contracts`, `make tests`, `make tests-full`, `make package-smoke`,
  and `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.
- Out of scope (#31): calling `_dispatch_host` from `cmd_repair_cycle`.

## File map

| File | Change |
|---|---|
| `desloppify/engine/repair_cycle.py` | add `DispatchRecord`; `CycleState.dispatch`, `attempt_history`; legacy decoding; `begin` archives |
| `desloppify/app/commands/repair_cycle_host.py` | add `host_session_id`, `HostReferences`, `HostLookupError`, `repository`, `worker_alive`, `worktrees`, `references`; tag line in prompt |
| `desloppify/app/commands/repair_cycle.py` | `_dispatch_host` intent/replay/return |
| `desloppify/tests/commands/test_repair_cycle.py` | engine and seam tests |
| `desloppify/tests/commands/test_repair_cycle_host.py` | adapter lookup tests |

## Task 1 — Dispatch record and attempt history (engine)

Interfaces produced:

```python
@dataclass(frozen=True)
class DispatchRecord:
    attempt_id: str
    phase: str                      # "intent" | "returned" | "unknown"
    session_id: str | None = None
    repository: str | None = None
    repo_root: str | None = None
    prior_worktrees: tuple[str, ...] = ()
    outcome: str | None = None
    pull_requests: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()
    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> DispatchRecord: ...
    def to_mapping(self) -> dict[str, object]: ...
    def with_references(self, pull_requests, issues, worktrees) -> DispatchRecord: ...
        # ordered union with the recorded tuples; never removes an entry

CycleState.dispatch: DispatchRecord | None = None
CycleState.attempt_history: list[dict[str, object]] = field(default_factory=list)
```

Verification:

- Mode: focused-test — round-trip, legacy decoding, and invalid shapes:
  `test_dispatch_record_round_trips_and_legacy_lease_is_unknown`. Red: import
  error for `DispatchRecord`. Green: `uv run --locked pytest
  desloppify/tests/commands/test_repair_cycle.py -k dispatch_record -q`.
- Mode: focused-test — `begin` archives the replaced attempt:
  `test_begin_archives_replaced_attempt`. Red: `attempt_history` missing.
  Green: same file, `-k archives`.

Steps:

1. Write the tests:
   - Round-trip a state with `DispatchRecord("a1", "returned", "s", "o/r", "/repo",
     ("/repo",), "completed", ("https://x/pull/1",), (), ("/w",))` through
     `json` and `from_mapping`.
   - `with_references(("p1", "p2"), (), ())` on a record holding `("p2", "p0")`
     yields `("p2", "p0", "p1")`.
   - A legacy mapping (lease present, no `dispatch` key) decodes to
     `DispatchRecord(lease.attempt_id, "unknown")`, keeps the lease, and an
     absent `attempt_history` decodes to `[]`.
   - `ValueError` for: `dispatch` with no lease; `dispatch.attempt_id` not the
     lease's; phase `"other"`; `pull_requests` not a list of strings;
     `attempt_history` not a list; a history entry without a decodable lease.
   - Archive: begin a1, `fail("x")`, `dispose("a1")`, set a `dispatch`, begin
     the next day; `attempt_history[0]` has the a1 lease mapping,
     `attempt_failure == "x"`, `disposed is True`, the dispatch mapping; the new
     state's `dispatch is None`. A second `begin` the same day returns `None`
     and leaves `attempt_history` at length 1.
2. Run, observe red.
3. Implement in `desloppify/engine/repair_cycle.py`:
   - `_DISPATCH_PHASES = frozenset({"intent", "returned", "unknown"})`.
   - `DispatchRecord.__post_init__` rejects empty `attempt_id` and unknown phase.
   - `from_mapping` uses `_required_text`, `_optional_text`, and a new
     `_texts(mapping, key) -> tuple[str, ...]` (absent -> `()`; must be a list of
     non-empty strings).
   - `CycleState.from_mapping`: decode `dispatch` per the spec's three rules;
     decode `attempt_history` with `_history(mapping)` which validates each
     entry's `lease` with `CycleLease.from_mapping` and non-null `dispatch` with
     `DispatchRecord.from_mapping`, returning `[dict(entry) ...]`.
   - `to_mapping` writes `"dispatch"` (mapping or `None`) and
     `"attempt_history"` (list copy).
   - `begin`: after the existing `_may_replace_lease` guard passes and when
     `current_lease` is not `None`, append `self._archive()` before replacing;
     set `self.dispatch = None` with the other resets.
   - `_archive()` returns the mapping named in the spec.
4. Run, observe green; run `make lint typecheck`; commit
   `feat(repair-cycle): persist dispatch records and attempt history`.

## Task 2 — Adapter lookups (host)

Interfaces produced (in `repair_cycle_host.py`):

```python
ATTEMPT_TAG = "Mending-Attempt"
def host_session_id(attempt_id: str) -> str: ...
class HostLookupError(RuntimeError): ...
@dataclass(frozen=True)
class HostReferences:
    pull_requests: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()
class ClaudeHostAdapter:
    def worker_alive(self, attempt_id: str) -> bool | None: ...
    def worktrees(self, repo_root: Path) -> tuple[str, ...]: ...
    def references(self, attempt_id: str, repository: str, repo_root: Path,
                   prior_worktrees: tuple[str, ...]) -> HostReferences: ...
```

Consumes existing `_marked_pids(marker: bytes) -> set[int] | None` and
`MARKER_VARIABLE`.

Verification:

- Mode: focused-test — `worker_alive`: a child started with
  `MENDING_HOST_SESSION=host_session_id(a)` -> `True`; none -> `False`;
  `_marked_pids` patched to `None` -> `None`. Red: attribute error.
- Mode: focused-test — `worktrees` and `references`: a fake `gh` on `PATH`
  prints JSON from env; a real `git` repo in `tmp_path`. `worktrees` returns the
  main checkout path; after `git worktree add -b feat/x ../wt` (no PR at all),
  `references(..., prior_worktrees=<before>)` returns `../wt` resolved. Only
  bodies with the exact `Mending-Attempt: <id>` line are kept; `gh` exiting 1,
  printing non-JSON, or missing from `PATH`, and `git` failing (non-repo root),
  raise `HostLookupError`. Red: attribute error.
- Mode: focused-test — prompt carries `Mending-Attempt: <id>` and `run` still
  uses `host_session_id` (extend `test_completed_run_reports_identity`).
- Green: `uv run --locked pytest desloppify/tests/commands/test_repair_cycle_host.py -q`.

Steps:

1. Write the tests above; run, observe red.
2. Implement: `host_session_id` returns
   `str(uuid.uuid5(uuid.NAMESPACE_URL, f"mending-attempt:{attempt_id}"))`;
   `run` calls it. `_prompt` adds
   `f"Put the line '{ATTEMPT_TAG}: {request.attempt_id}' in the body of every issue and pull request you create.\n"`.
   `worker_alive` builds the marker bytes as `run` does and maps `_marked_pids`.
   `worktrees` runs `git -C <repo_root> worktree list --porcelain` and returns
   the `worktree <path>` values. `references` calls
   `_tagged(["gh", "pr", "list", ...])` and `_tagged(["gh", "issue", "list", ...])`
   (`--repo <repository> --state all --limit 100 --json url,body`), keeping
   entries whose `body` string has the exact tag line, plus the current
   worktrees not in `prior_worktrees`. One helper `_output(argv) -> str` runs
   `subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)`
   after resolving `argv[0]` with `shutil.which`, raising `HostLookupError` on a
   missing executable, `OSError`, `TimeoutExpired`, or non-zero exit; a non-list
   JSON payload also raises.
3. Green; `make lint typecheck`; commit
   `feat(repair-cycle-host): look up a dispatched attempt's worker and references`.

## Task 3 — Dispatch seam (`_dispatch_host`)

Consumes Task 1's `DispatchRecord`/`CycleState.dispatch` and Task 2's
`host_session_id`, `HostLookupError`, `worker_alive`, `references`. The test
fake gains `worker_alive` and `references` methods with configurable results.

Verification:

- Mode: focused-test — replay never calls `run`; parametrized cases, all after
  the lease deadline: alive True -> park `dispatch-in-flight`; None ->
  `dispatch-unverified`; returned + lookup error -> park
  `dispatch-lookup-unavailable`; held reservation (with and without lookup
  error) -> fail `unsettled-reservation`; phase unknown (with and without lookup
  error) -> fail `dispatch-outcome-unknown`; returned -> park
  `already-dispatched`. The fake records the repository/root it was asked for
  (must be the record's), and a pre-populated reference survives a lookup that
  returns fewer. Red: `run` called.
- Mode: focused-test — intent on disk inside `run`
  (extend `test_dispatch_persists_reservation_before_launch`): phase `intent`,
  `session_id == host_session_id("a1")`, repository, root, prior worktrees.
- Mode: focused-test — returned phase and settled reservation on disk inside
  the fake `references` called after `run`.
- Mode: focused-test — `parked` outcome leaves `dispatch is None`; a snapshot
  `HostLookupError` parks `dispatch-lookup-unavailable` with nothing reserved;
  a lookup error after return leaves references unchanged.
- Existing `test_interrupted_dispatch_keeps_reservation` stays green through the
  replay path.
- Green: `uv run --locked pytest desloppify/tests/commands/test_repair_cycle.py -q`.

Steps:

1. Write the tests; run, observe red.
2. Implement in `repair_cycle.py`: after the lease-match check,
   `if cycle_state.dispatch is not None: _replay_dispatch(state, cycle_state, adapter, request); return None`.
   Before admission, `prior = adapter.worktrees(request.repo_root)` (park
   `dispatch-lookup-unavailable` on `HostLookupError`). After admission, set
   `cycle_state.dispatch = DispatchRecord(lease.attempt_id, "intent",
   host_session_id(lease.attempt_id), config_repository, str(request.repo_root), prior)`
   before the existing `_store_cycle_state`/`_persist_before_external_call`.
   The repository comes from `adapter.repository` (a read-only property
   returning `self._config.repository`, added in this task).
   After `run`: parked -> `cycle_state.dispatch = None`; otherwise
   `replace(record, phase="returned", outcome=outcome.state)`, settle as in
   #29, store, `_persist_before_external_call`, then `_add_references(...)`
   (ignores `HostLookupError`). `_replay_dispatch` follows the spec's order.
3. Green; full guardrails; commit
   `feat(repair-cycle): replay a recorded dispatch instead of relaunching`.
