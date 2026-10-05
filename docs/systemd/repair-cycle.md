# Bounded repair-cycle systemd recipe

This is the only supported scheduler recipe. It runs one `desloppify
repair-cycle` command from a systemd timer; it is not a resident service.
The timer invokes the same bounded one-shot path as a manual run
([ADR 0007](../adr/0007-host-driven-maintenance.md)). Do not activate it until
the manual pilot (#7) succeeds and the operator approves the recurring scope,
window, and limits.

## Install

Install `desloppify` at `/usr/local/bin/desloppify`. Create the dedicated
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
90-minute and 100-call defaults.

```json
{
  "enabled": true,
  "repository": "OWNER/REPOSITORY",
  "model": "SELECTED_ORCHESTRATOR_MODEL",
  "runtime_minutes": 90,
  "call_limit": 100,
  "cost_cap_usd": "25.00"
}
```

The command enforces the selected runtime while each adapter call is running,
then rechecks the lease deadline before accepting its receipt. A deadline
overrun parks the recorded attempt; it never starts another selection.

Set `enabled` to `false` to park new work while retaining enough local state to
reconcile an already-recorded attempt. A missing model or positive USD cap also
parks before model work. Only USD-measured usage is accepted; another currency
parks instead of being converted.

## Window and activation

Edit `OnCalendar` only in `mending-repair-cycle.timer` to choose the daily
window. With no timezone suffix, systemd interprets it in the host system
timezone. Do not add a second window check to configuration. `Persistent=false`
deliberately skips missed windows rather than catching them up.

After the pilot and that approval, copy the units, create the restricted
files, and activate the timer:

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
