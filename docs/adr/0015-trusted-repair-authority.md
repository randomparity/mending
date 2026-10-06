# 0015 — Read repair authority from the trusted cycle configuration

## Status

Accepted (2026-10-06)

## Context

`repair-cycle` asks the `AdeptCycleClient` for an `AuthorityProof` and accepts
any proof whose repository matches and whose other fields are non-empty
strings. It checks once, before selection, and binds nothing to the selected
brief. ADR 0007 moved authority to explicitly trusted operator configuration
but left its form and check sites open; ADR 0014 stores a reviewed-brief
version for #26 to bind to. The coding host runs as the same account as the
cycle (ADR 0010), so anything that account can write, the model can write.

## Decision

- **Source.** Authority is the `authority` object of the repair-cycle
  configuration file: `schema` (1), `revision`, and `approvals`. The file and
  every directory above it must be neither owned nor writable by the account
  running the cycle, since an owner can make a file writable again, and the
  path must hold no symlink or `..` component; otherwise
  every check parks as `authority-untrusted`. Nothing else grants authority:
  no client, issue, model output, state-file field, or proof string.
- **Approval.** One approval names a stable concern `key`, its
  `reviewed_brief_version` and `evidence_digest`, the `files` and `actions`
  it allows (`repair` is the only supported action), `call_limit`,
  `cost_cap_usd`, `runtime_minutes`, a required `expires_at`, and `revoked`.
  The configuration's `repository` binds it to one repository.
- **Selection.** The cycle binds a published small repair: an open work item
  whose `github_repair` link is not closed, preferring one with an approval,
  then ADR 0014's rank order. It authorizes before creating the lease, so a
  refusal leaves no attempt behind.
- **One check.** `check_authority(authority, binding, now, bound)` in
  `desloppify/engine/repair_authority.py` compares an approval with the
  binding built from the current work item: its recomputed reviewed-brief
  version, evidence digest, manifest files, the `repair` action, and the
  attempt's limits (configured at selection, the lease's afterwards).
  `repair-cycle` calls it at selection, before host
  dispatch, and on resume of an attempt reported active; each call first
  reruns repair-queue's source comparison (ADR 0008) on that work item.
- **Distinct reasons.** `authority-untrusted`, `authority-invalid`,
  `authority-unsupported`, `authority-missing`, `authority-revoked`,
  `authority-expired`, `authority-mismatch`, `authority-scope-exceeded`,
  `authority-limits-exceeded`, plus `source-not-current`,
  `source-unreadable`, and `selected-repair-unavailable`.
- **Revocation.** Selection records the work item, key, and authority
  revision on the attempt. Later checks rebuild the binding from that work
  item; with nothing recorded they refuse as `authority-missing`. A removed
  approval or `revoked: true` is `authority-revoked`. The recorded revision
  is for audit only: it sits in the state file, which the host can write, so
  a revision change alone revokes nothing. A refusal at selection or before
  dispatch parks; a refusal on resume fails the attempt, so new work waits
  for a disposition, and a resume check that times out parks as
  `observation-timeout`.
- **Disposition.** Disposing an attempt last reported active (an `active`
  receipt, or a dispatch at `intent` or `unknown`, or with an `unknown`
  outcome) needs `--confirm-stopped`.

## Consequences

Selection now requires a published repair and an approval for its current
brief; without them the cycle parks before creating a lease or calling the
client. A material brief or evidence change voids the approval bound to the
old version. Running the cycle as root, or from a configuration the service
account owns, always parks. Observation (ADR 0007, #28) is unaffected by
refusal. The file allowlist is checked against the brief's manifest; the
host's actual edits are checked by the composed cycle (#31). An active attempt
recorded before this change has no bound authority and fails on resume, and
so does one whose source files change in the checkout before it ends (for
example, its repair landing there); the operator disposes of it.

## Considered & rejected

- **Keep the client-supplied proof (do nothing).** verified: `_valid_authority`
  in `desloppify/app/commands/repair_cycle.py` at `8ad97b6` accepts any
  non-empty strings, which #26 rules out.
- **A separate authority file.** judgment: complexity; a second restricted
  file with the same owner and mode adds a path without adding trust.
- **Signed approvals.** judgment: cost; public-key verification needs a new
  crypto dependency and operator key management, while a root-owned file
  already excludes the service account in the supported deployment.
- **Bind to `repair_queue_selection`.** verified: `_record_selection` in
  `desloppify/app/commands/repair_queue.py` at `8ad97b6` replaces the record on
  every `sync --apply`, and a linked candidate is no longer selectable, so the
  next sync records `no-op` for an approved repair.
- **Revoke by revision change.** judgment: fit; the bound revision would be
  compared from the host-writable state file, so only fields in the trusted
  file can revoke.
- **Host permission settings as authority.** verified: ADR 0010 uses the
  host account's Claude settings for tool permissions; they name no brief,
  evidence version, or limit.
- **Trust the stored link version.** verified: ADR 0014 Consequences say a
  stored version can lag the work item, so the binding recomputes it.
