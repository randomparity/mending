# Bind repair-cycle authority to the selected brief

Issue: #26 (part of #19). Decision record:
[ADR 0015](../../adr/0015-trusted-repair-authority.md). Builds on ADRs 0007,
0008, 0010, and 0014.

## Problem

`repair-cycle` takes an `AuthorityProof` from `AdeptCycleClient` and accepts
any non-empty strings (`_valid_authority`). It checks once before selection,
binds nothing to the selected brief, evidence, files, actions, or limits,
never rechecks before dispatch or on resume, and lets an operator dispose an
attempt still reported active.

## Scope

**Engine** — new `desloppify/engine/repair_authority.py`:

- `Approval`, `Authority(repository, revision, approvals)`, and
  `AuthorityBinding(repository, key, reviewed_brief_version, evidence_digest,
  files, action, call_limit, cost_cap_usd, runtime_seconds)`, frozen.
- `authority_from_mapping(repository, value) -> Authority | None`: `None` for
  an absent block; raises `UnsupportedAuthority` (a `ValueError`) when
  `schema != 1` or an approval names an action other than `repair`, and
  `ValueError` for any malformed field or a duplicate key. `expires_at` must be a
  timezone-aware ISO-8601 time; `files` and `actions` are non-empty string
  lists; limits are positive.
- `check_authority(authority, binding, now, bound=False) -> str | None`,
  first match wins: block absent or no approval for the key →
  `authority-revoked` when `bound`, else `authority-missing`;
  `authority-mismatch` when the repository, brief version, or evidence
  digest differs; `authority-revoked` when `revoked` is true;
  `authority-expired` when `now >= expires_at`; `authority-scope-exceeded`
  when the action is not allowed or a binding file is not allowed;
  `authority-limits-exceeded` when any binding limit exceeds the approval's.

**Configuration** — `CycleConfig.authority` keeps the raw `authority` value;
it is decoded only by the check, so a bad block parks new work but never
blocks loading the configuration or observing an attempt.

**State** — `CycleState.authority`: `None` or `{issue_id, key, revision}`,
validated on load, reset by `begin`, kept in the attempt archive.
`CycleState.dispose(attempt_id, *, confirm_stopped=False)` raises unless
`confirm_stopped` when the receipt state is `active` or the dispatch record
is at `intent` or `unknown` or has outcome `unknown`.

**Command** (`desloppify/app/commands/repair_cycle.py`):

- `_authorize(args, state, cycle_state, config, now, *, select=False)
  -> (AuthorityBinding, bound record) | str`: unbound and not `select` →
  `authority-missing`. Trust check (`authority-untrusted` when the resolved
  `--config` file or any ancestor is owned by `os.geteuid()` or writable per
  `os.access`, or cannot be stat'ed, or the absolute path differs from its
  resolved form; in-process `config_data` is trusted),
  then decoding (`authority-unsupported` / `authority-invalid`). The work
  item is the bound one, or under `select` the minimum over open small
  repairs whose `github_repair` link is not `closed` of (no approval for its
  key, `rank_key`, id). No item, candidate, or version →
  `selected-repair-unavailable`; a bound key that differs →
  `authority-mismatch`. `source_comparison` (renamed from
  `repair_queue._comparison`): not current → `source-unreadable` when it
  says keep, else `source-not-current`. Then `check_authority`, with the
  configured limits at selection and the lease's (remaining runtime)
  afterwards; no cost cap → `missing-cost-cap`.
- `_begin_and_select` runs `_authorize(select=True)` within the runtime bound
  before `begin`; a refusal or timeout parks with no new lease. Success sets
  `cycle_state.authority`, persists, and `_select` passes the binding to
  `client.select`. `verify_authority`, `AuthorityProof`, and
  `_valid_authority` are removed.
- `_dispatch_host(args, state, cycle_state, config, adapter, request)` parks
  any `_authorize` refusal after the reservation and deadline checks and
  before lookup. Replay of a recorded dispatch stays observation only.
- `_reconcile`: after an accepted `active` receipt, `_authorize` runs within
  the observation bound; a refusal calls `_fail`, a timeout parks
  `observation-timeout`.
- Parser: `--confirm-stopped`, passed to `dispose`.

**Docs** — `docs/systemd/repair-cycle.md`: the authority block, its trust
requirement, revocation, and `--confirm-stopped`.

## Failure model

1. Actors and deployments: the operator editing the configuration; the
   systemd service account (ADR 0006) or a manual pilot account; the coding
   host and its model, running as that account and writing the checkout.
2. Invariants and assets: no host dispatch or new selection without a
   current approval; revocation observed on the next check; persisted state
   readable after upgrade; observation never blocked by authority.
3. Accepted: a configuration the account owns or can write (parked, not
   prevented); an approval edited between two checks in one run (seen at the
   next check); the host rewriting the bound record in the state file (it can
   only rebind to another approved, current brief, or lose its binding and
   be refused); an active attempt failed because its source changed in the
   checkout, including by its own repair landing (operator disposes).
4. Covered elsewhere: the host's actual edits against `files`, windows, and
   the composed dispatch → #31; observation after revocation → #28; host
   permissions → ADR 0010; contract ADR → #16.

## Threat model

- Boundaries: configuration file → cycle (existing, now carries authority);
  work item and repair-queue selection in the state file → binding (widened:
  now gates dispatch); checkout blobs → source comparison (existing).
- Actors: the model on the host (untrusted, same account, writes checkout,
  state file, and anything that account owns); the operator (trusted).
- Controls: file and ancestors neither owned nor writable by this account;
  approval must match the recomputed brief version and evidence digest, so
  editing a work item or state cannot match an approval it did not earn;
  revocation reads only the trusted file; source comparison
  reruns against git blobs; every refusal parks with a reason and leaks no
  configuration values.
- Out of scope: a compromised operator or root; an attacker who can write the
  configuration's directory as another user.

## Success

1. Each reason above has a test that reaches it from `cmd_repair_cycle` or
   `check_authority`, and no client selection, dispatch, or launch happens
   after a refusal.
2. Selection, dispatch, and resume each call `_authorize`.
3. A removed approval or `revoked: true` after selection is refused before
   dispatch and fails an active attempt on resume; so does an unbound one.
4. Dispose refuses an active attempt without `--confirm-stopped`.
5. Existing state without `authority` loads.
