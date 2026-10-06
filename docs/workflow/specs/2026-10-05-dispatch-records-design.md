# Repair-cycle dispatch records design

Issue #30, part of #19. No new decision record: ADR 0010 already derives the
host session ID from the durable attempt ID, makes it single-use, and states
that dispatch records extend its adapter; ADR 0007 keeps the persisted lease as
Mending's attempt record. This change makes that record cover the dispatch
itself and keeps it after the attempt is replaced.

## Problem

`_dispatch_host` (`desloppify/app/commands/repair_cycle.py`, from #29) persists
the lease and a budget reservation before it launches the host, but nothing
says a dispatch was intended or which host session, worktree, issue, or PR it
produced. A crash between launch and settlement leaves only an unsettled
reservation. A later call has no way to find the orphaned worker (the host runs
in its own session and survives Mending) or the PR it opened, and nothing stops
a second `_dispatch_host` call for the same attempt from launching again.
`CycleState.begin` also resets the receipt, failure, and disposition of the
attempt it replaces, so a disposed attempt leaves no history.

## Scope

No ownership transition: a clean extension of `CycleState`
(`desloppify/engine/repair_cycle.py`), `ClaudeHostAdapter`
(`desloppify/app/commands/repair_cycle_host.py`), and the `_dispatch_host`
seam. Wiring that seam into `cmd_repair_cycle`, and preventing a *new* attempt
from duplicating an open repair PR, belong to #31 (configured window; "an open
repair PR awaiting disposition keeps the repair active").

### Dispatch record (engine)

`DispatchRecord` is a frozen dataclass persisted as `CycleState.dispatch`:

| Field | Meaning |
|---|---|
| `attempt_id` | the lease's attempt ID; must equal the current lease's |
| `phase` | `intent` (persisted before launch), `returned` (the adapter returned an outcome), or `unknown` (legacy lease, see below) |
| `session_id` | the host session ID derived from the attempt ID; `None` for `unknown` |
| `repository`, `repo_root` | the `config.repository` and `request.repo_root` the dispatch used; replay looks up with these, not the caller's; `None` for `unknown` |
| `prior_worktrees` | the repository's worktree paths read just before launch |
| `outcome` | the `HostOutcome.state` once `returned`, else `None` |
| `pull_requests`, `issues`, `worktrees` | URLs/paths found for the attempt; empty until looked up; each lookup adds to them (ordered union) and never removes |

Decoding rules (`CycleState.from_mapping`):

- `dispatch` key present: `null` means no dispatch recorded; a mapping must
  decode and match the current lease's attempt ID, and a record with no lease is
  invalid.
- `dispatch` key absent and a lease present (state written before this change):
  the record decodes as `DispatchRecord(<lease attempt>, "unknown")`. The next
  save writes it explicitly. The lease itself is untouched, so the existing
  reconcile/disposition rules still govern it and it is never discarded.
- `dispatch` key absent and no lease: no dispatch recorded.

### Attempt history (engine; operator scope addition)

`CycleState.attempt_history` is a list of mappings, oldest first. Once
`_may_replace_lease` allows `begin` to replace a lease (never on a refused
`begin`), it first appends the replaced attempt's facts: `lease`,
`authoritative_receipt`, `parked_reason`, `attempt_failure`, `disposed`
(boolean), `observation_calls`, `consumed_cost_usd`, `consumed_calls`,
`reserved_cost_usd`, `reserved_calls`, and `dispatch` (mapping or `null`). It
then resets the current-attempt fields as today and sets `dispatch` to `None`.
Decoding requires a list whose entries are mappings with a decodable `lease`
and, when non-null, a decodable `dispatch`; absent means an empty list. The
list is not bounded: `begin` replaces at most one lease per host-local day.

### Adapter lookup (host)

- `host_session_id(attempt_id)` is the existing `uuid5` derivation, moved to a
  module function that `run` also uses.
- The prompt gains one line asking the host to put `Mending-Attempt: <attempt
  ID>` on its own line in the body of every issue and pull request it creates.
- `worker_alive(attempt_id) -> bool | None`: whether any process carries the
  attempt's `MENDING_HOST_SESSION` marker (the existing `/proc` scan, which also
  covers the host leader); `None` when `/proc` is unavailable. Local only.
- `worktrees(repo_root) -> tuple[str, ...]`: the `worktree` paths from
  `git -C <repo_root> worktree list --porcelain`.
- `references(attempt_id, repository, repo_root, prior_worktrees) ->
  HostReferences(pull_requests, issues, worktrees)`: `gh pr list` and
  `gh issue list --repo <repository> --state all --limit 100 --json url,body`,
  keeping entries whose body has the exact tag line, and every current worktree
  not in `prior_worktrees`. The worktree half needs no PR and no host
  cooperation: one repair runs at a time on the dedicated checkout (ADR 0006),
  so a worktree that appeared during the dispatch is the attempt's. A listing,
  not GitHub search, so a PR created seconds before a crash is not missed by
  search-index lag.
- Each command runs with fixed argv and a 10-second timeout. A missing `gh`,
  a non-zero exit, a timeout, or unparseable output raises `HostLookupError`.

### Dispatch flow (`_dispatch_host`)

After the existing lease-match check:

1. **Replay.** If `cycle_state.dispatch` is not `None`, never call `run`:
   - `worker_alive` is `True` -> park `dispatch-in-flight`; `None` -> park
     `dispatch-unverified`. Nothing else is looked up or charged, and no
     failure is recorded even when a reservation is held, so replay itself never
     makes the attempt disposable while its worker may run; the first replay
     after it exits records the failure. Failures that other paths record
     (reconcile and receipt limits) stay disposable as today; refusing a new
     attempt while a recorded worker lives belongs to #31's composition.
   - Otherwise call `references` with the record's repository, root, and prior
     worktrees (an `unknown` record has none, so it uses the request's root,
     the config's repository, and no prior worktrees, and records no worktrees
     because without a pre-launch snapshot every worktree would be
     misattributed) and add what it finds to the record. A `HostLookupError`
     adds nothing.
   - Then: a reservation still held -> fail `unsettled-reservation` (#29's
     reason, spend unknown); phase `unknown` -> fail
     `dispatch-outcome-unknown`; a `HostLookupError` -> park
     `dispatch-lookup-unavailable` (retryable); otherwise park
     `already-dispatched`. A failure requires `--dispose-attempt` before new
     work, as today, so a lookup outage never strands an attempt.
   Replay runs before the deadline and admission checks: it is an observation,
   and it must work after the lease expires.
2. The existing unsettled-reservation and deadline checks.
3. **Snapshot.** Call `worktrees(request.repo_root)`; a `HostLookupError`
   parks `dispatch-lookup-unavailable` before anything is admitted.
4. The existing admission check.
5. **Intent.** Set `dispatch = DispatchRecord(attempt, "intent", session ID,
   repository, resolved repo root, prior worktrees)` and persist it in the same
   `save_state` as the reservation, before `run`. The intent precedes every
   external side effect (host launch, publication); the only earlier I/O is the
   read-only local snapshot it records, and the lease carrying the attempt
   correlation was persisted by `begin` before that.
6. **Return.** A `parked` outcome launched nothing, so `dispatch` returns to
   `None` (the attempt may dispatch again, as #29 allows). Any other outcome
   sets phase `returned` and its `outcome`, settles as in #29, and persists
   before any lookup. Only then does it call `references` and add what it
   finds; a `HostLookupError` there leaves the lists as they are and changes
   nothing else.

A `run` that raises (cancellation) leaves the `intent` record and the
reservation persisted, so the next call takes the replay path.

## Failure model

1. **Actors and deployments**
   - A local operator or the systemd unit running `desloppify repair-cycle`
     on the Linux repair host (ADR 0006, ADR 0010), one process at a time under
     the state-file lock, as the account that launched the host: the unit's
     state directory is `mending`-owned with mode 0700
     (`docs/systemd/mending-repair-cycle.service`), so only that account or
     root (which can read every process environment) can replay.
   - The Claude Code host, its worker tree, and the Adept skills it runs,
     acting through the host account's `gh` credentials.
2. **Invariants and assets at stake**
   - No second host launch for an attempt whose dispatch record exists (a
     duplicate worker spends money and can open a duplicate issue or PR).
   - The intent is on disk before `run`; a crash at any point after that leaves
     a record the next call replays.
   - Existing state decodes; a legacy lease is retained and treated as an
     unknown dispatch outcome.
   - A replaced attempt's facts survive in `attempt_history`.
3. **Accepted failure classes**
   - The host ignores the tag instruction: lookup finds no issue/PR, so
     references stay empty. Bounded: replay still never relaunches, and the
     attempt fails or parks for the operator.
   - An issue/PR older than the 100 most recent is not found by the first
     lookup after the worker exits (the return-path lookup, or the first replay
     that sees no live worker). Bounded: references found earlier are never
     removed, and the cost is a missing reference, not a duplicate.
   - A worktree another actor creates in the dedicated checkout during the
     dispatch is attributed to the attempt. Bounded: one repair runs at a time
     there (ADR 0006), and the cost is an extra path in the record.
   - A worker that clears its environment and leaves the process group is not
     seen by `worker_alive` (ADR 0010, Consequences).
   - A post-return lookup failure leaves the reference lists as they were; the
     outcome and settlement are already on disk.
   - Running a build from before this change against state this change wrote
     drops `dispatch` and `attempt_history` on its next save. Bounded: a
     re-upgrade decodes the lease as `unknown` and never relaunches; downgrades
     are outside the named deployments.
4. **Covered elsewhere**
   - Composing `_dispatch_host` into `repair-cycle`, and one active repair per
     window with an open PR keeping the repair active: #31.
   - Stopping an orphaned worker found alive after its lease expired:
     observation allowance (#28) and composition (#31) decide when to observe
     again; this change only parks.
   - Authority recheck before dispatch: #26.

### Threat model

1. **Boundary inventory** — added: Mending reads `gh pr list`/`gh issue list`
   output (issue/PR bodies written by the host or anyone with write access) and
   `git worktree list` output; replay passes the state-recorded `repository`
   and `repo_root` as `gh --repo` and `git -C` values; the `/proc` marker scan
   now gates replay; lookup errors print up to 200 characters of `gh`/`git`
   stderr to stdout. Widened: none.
2. **Actor model** — the host model and anyone able to open issues/PRs in the
   configured repository can write bodies containing a tag. Trust stays with
   the operator's configuration, the local state file (0700, owned by the
   repair account), and same-account processes.
3. **Control per boundary** — `gh` and `git` run as list argv (no shell) whose
   only variable values are config- or state-sourced option arguments; the
   attempt ID is a Mending-generated value used only for exact string
   comparison. JSON is parsed with `json.loads` and only `url` and `body`
   strings are read; non-string or missing fields are skipped. The marker scan
   matches whole NUL-delimited environment entries. Error text is a bounded
   stderr tail; `gh` and `git` do not print credentials there.
   References are recorded, never executed or used to authorize anything, so a
   forged tag can at most add a URL to the record.
4. **Explicitly out of scope** — authenticating who wrote a tagged body:
   references are recovery hints for the operator, not authority.

## Validation

| Contract | Evidence |
|---|---|
| Dispatch record and history round-trip; legacy decoding; invalid shapes rejected | focused engine tests in `test_repair_cycle.py` |
| `begin` archives the replaced attempt; a refused `begin` leaves history unchanged | focused engine test |
| Intent (with repository, root, prior worktrees) persisted before `run` | on-disk read inside a fake `run` |
| Returned phase and settlement persisted before the post-return lookup | on-disk read inside a fake `references` |
| Replay never calls `run`: alive, unverified, lookup failure (returned / held reservation / unknown), unsettled, unknown (no worktrees recorded), already dispatched; uses recorded repository and root; references only grow | parametrized `_dispatch_host` test with a fake host |
| Parked outcome clears the record; snapshot failure admits nothing | `_dispatch_host` tests |
| `worker_alive`, `worktrees`, `references` (tagged bodies, new worktree with no PR), tag line in prompt, session-ID derivation | `test_repair_cycle_host.py` with a fake `gh` on `PATH`, a real `git` worktree, and a marked child process |

Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
`make tests`, `make tests-full`, `make package-smoke`.
