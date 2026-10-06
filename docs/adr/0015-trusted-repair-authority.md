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
  its directory must not be writable by the account running the cycle;
  otherwise every check parks as `authority-untrusted`. Nothing else grants
  authority: no client, issue, model output, or proof string.
- **Approval.** One approval names a stable concern `key`, its
  `reviewed_brief_version` and `evidence_digest`, the `files` and `actions`
  it allows (`repair` is the only supported action), `call_limit`,
  `cost_cap_usd`, `runtime_minutes`, a required `expires_at`, and `revoked`.
  The configuration's `repository` binds it to one repository.
- **One check.** `check_authority(authority, binding, now, bound_revision)`
  in `desloppify/engine/repair_authority.py` compares an approval with the
  binding built from the current work item: its recomputed reviewed-brief
  version, evidence digest, manifest files, the `repair` action, and the
  configured limits. `repair-cycle` calls it at selection, before host
  dispatch, and on resume of an attempt reported active; each call first
  reruns repair-queue's source comparison (ADR 0008) on that work item.
- **Distinct reasons.** `authority-untrusted`, `authority-invalid`,
  `authority-unsupported`, `authority-missing`, `authority-revoked`,
  `authority-expired`, `authority-mismatch`, `authority-scope-exceeded`,
  `authority-limits-exceeded`, plus `source-not-current`,
  `source-unreadable`, and `selected-repair-unavailable`.
- **Revocation.** Selection records the work item, key, and authority
  revision on the attempt. Later checks treat a changed revision, a removed
  approval, or `revoked: true` as `authority-revoked`. A refusal at
  selection or before dispatch parks; a refusal on resume fails the attempt,
  so new work waits for a disposition.
- **Disposition.** Disposing an attempt last reported active (an `active`
  receipt, or a dispatch still at `intent` or with an `unknown` outcome)
  needs `--confirm-stopped`.

## Consequences

Selection now requires a `repair-queue` selection record and an approval for
its current brief; without them the cycle parks before any client call. A
material brief or evidence change, or a new revision, voids every approval
bound to the old one. Running the cycle as root, or from a configuration the
service account owns, always parks. Observation (ADR 0007, #28) is unaffected
by refusal. The file allowlist is checked against the brief's manifest; the
host's actual edits are checked by the composed cycle (#31). An attempt
recorded before this change has no bound authority and fails on resume.

## Considered & rejected

- **Keep the client-supplied proof (do nothing).** verified: `_valid_authority`
  in `desloppify/app/commands/repair_cycle.py` at `8ad97b6` accepts any
  non-empty strings, which #26 rules out.
- **A separate authority file.** judgment: complexity; a second restricted
  file with the same owner and mode adds a path without adding trust.
- **Signed approvals.** judgment: cost; the signing key would sit where the
  service account can read it, so it proves no more than file ownership.
- **Host permission settings as authority.** verified: ADR 0010 uses the
  host account's Claude settings for tool permissions; they name no brief,
  evidence version, or limit.
- **Trust the stored link version.** verified: ADR 0014 Consequences say a
  stored version can lag the work item, so the binding recomputes it.
