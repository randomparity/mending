# Bounded repair-cycle systemd recipe

This is the only supported scheduler recipe. It runs one `desloppify
repair-cycle` command from a systemd timer; it is not a resident service.
The timer invokes the same bounded one-shot path as a manual run
([ADR 0007](../adr/0007-host-driven-maintenance.md)). Do not activate it until
the manual pilot (#7) succeeds and the operator approves the recurring scope,
window, and limits.

## Install

Perform these steps only after the pilot and approval above. Install
`desloppify` at `/usr/local/bin/desloppify`. Create the dedicated
unprivileged `mending` account and make it the owner of the one target checkout
at `/var/lib/mending/repository`. Copy the two unit files in this directory to
`/etc/systemd/system/`.

Create `/etc/mending/repair-cycle.env`, owned by `root:mending` and mode `0640`:

```text
MENDING_REPAIR_CYCLE_CONFIG=/etc/mending/repair-cycle.json
```

Create `/etc/mending/repair-cycle.json`, owned by `root:mending` and mode
`0640`. The operator selects the repository identity, model, runtime, call
limit, and USD cost cap. Runtime and call limit may be omitted to use their
90-minute and 100-call defaults. `observation_call_limit` (default 3) and
`observation_minutes` (default 5) bound how a recorded attempt is observed; see
below.

```json
{
  "enabled": true,
  "repository": "OWNER/REPOSITORY",
  "model": "SELECTED_ORCHESTRATOR_MODEL",
  "runtime_minutes": 90,
  "call_limit": 100,
  "cost_cap_usd": "25.00",
  "authority": {
    "schema": 1,
    "revision": "2026-10-06.1",
    "approvals": [
      {
        "key": "CONCERN_KEY",
        "reviewed_brief_version": "REVIEWED_BRIEF_VERSION",
        "evidence_digest": "EVIDENCE_DIGEST",
        "files": ["src/module.py"],
        "actions": ["repair"],
        "call_limit": 100,
        "cost_cap_usd": "25.00",
        "runtime_minutes": 90,
        "expires_at": "2026-10-13T00:00:00+00:00"
      }
    ]
  }
}
```

`authority` is the only source of repair authority
([ADR 0015](../adr/0015-trusted-repair-authority.md)). Each approval names one
published repair by its stable key and the `reviewed_brief_version` and
`evidence_digest` stored on its `github_repair` link record in the state file.
It allows the files of the brief's evidence manifest, the `repair` action
(the only one supported), limits at or above the cycle's own, and an expiry.
A selection binds the attempt to the approved repair and records the
`revision` for audit. Before
dispatch, and whenever a recorded attempt is observed still active, the cycle
checks again: it recomputes the brief version, rechecks the source files and
their evidence, and re-reads the approval. To revoke, set `"revoked": true`
or remove the approval; changing `revision` alone revokes nothing. A
revocation stops dispatch and
fails an active attempt, which then needs a disposition. A changed brief or
evidence version voids its approval the same way, and an active attempt also
fails when its source files change in the checkout (including when its own
repair lands there) or when it was recorded before authority existed. Every refusal parks with a
named reason (`authority-missing`, `authority-revoked`, `authority-expired`,
`authority-mismatch`, `authority-scope-exceeded`, `authority-limits-exceeded`,
`authority-invalid`, `authority-unsupported`, `authority-untrusted`,
`source-not-current`, `source-unreadable`, or `selected-repair-unavailable`).

The configuration grants authority only when the account running the cycle
neither owns nor can write it, or any directory above it, and `--config` is
not a symbolic link or under one; otherwise every run parks as
`authority-untrusted`. The root-owned files above meet this. Never
run the cycle as root, and keep a manual pilot's configuration in a directory
owned by another account.

The command enforces the selected runtime while each authority or selection
call is running. Reading a recorded attempt's outcome is separate: each run may
make one read-only reconcile call, up to `observation_call_limit` calls per
attempt, each bounded by `observation_minutes`, even after the runtime has
expired. It never extends the runtime or starts work. A receipt observed after
the deadline, or over the call or cost limit, is recorded with its usage and
the attempt is marked failed (`runtime-exhausted` or `budget-exhausted`).
Because receipts carry no completion time, any interrupted attempt first
observed after its deadline is marked failed this way. When the allowance is
spent, runs park as `observation-exhausted` and no further reads occur.

The state file's `attempt_failure` keeps the attempt's first failure. While it
is set, no new work starts: a terminal attempt parks later runs as
`disposition-required`, and a still-active one keeps parking with its
observation reason. Review the attempt, then record a disposition:

```sh
desloppify repair-cycle --config /etc/mending/repair-cycle.json \
  --state /var/lib/mending-repair-cycle/state.json --dispose-attempt ATTEMPT_ID
```

The attempt ID is `current_lease.attempt_id` in the state file. Disposing makes
no external call and cancels nothing: it asserts the attempt has stopped or is
abandoned. If the recorded receipt (`authoritative_receipt.state`) is `active`,
or a host dispatch is unsettled (`dispatch.phase` is `intent` or `unknown`, or
`dispatch.outcome` is `unknown`), disposal is refused until you confirm on the
host that the attempt's worker and pull request are finished and add
`--confirm-stopped`; otherwise a second repair can run beside it. If the
receipt is absent, make the same check before disposing. The next timer window on a
later day may start new work.

Set `enabled` to `false` to park new work while retaining enough local state to
reconcile an already-recorded attempt. A missing model or positive USD cap also
parks before model work. Only USD-measured usage is accepted; another currency
parks instead of being converted.

## Window and activation

Edit `OnCalendar` only in `mending-repair-cycle.timer` to choose the daily
window. With no timezone suffix, systemd interprets it in the host system
timezone. Do not add a second window check to configuration. `Persistent=false`
deliberately skips missed windows rather than catching them up.

After copying the units and creating the restricted files, activate the
timer:

```sh
systemctl daemon-reload
systemctl enable --now mending-repair-cycle.timer
systemctl list-timers mending-repair-cycle.timer
```

Inspect a completed attempt with `systemctl status mending-repair-cycle.service`
and `journalctl -u mending-repair-cycle.service`. Disable safely with:

```sh
systemctl disable --now mending-repair-cycle.timer
```

Re-enabling invokes a later timer window; it does not replay a skipped one. If
the scheduler finds an unresolved recorded attempt, it reconciles that attempt
before any new selection. An already-recorded terminal receipt needs no second
external reconciliation.

## Host adapter and pilot

Repairs run through a Mending-owned adapter for one concrete coding host, which
runs the installed Adept skills; Adept is not a service. That adapter is not
implemented yet (#19). Until it is, the command parks before selection. Target
opt-in, the manual pilot, and timer activation remain #7 responsibilities, and
merge is outside the pilot.
