# 0016 — Compose the repair cycle and settle attempts by their pull requests

## Status

Accepted (2026-10-06)

## Context

ADR 0007 makes one `repair-cycle` invocation compose discovery, selection,
authority, and one host dispatch, with "one active repair and at most one newly
dispatched repair per configured window" and no merge permit. The code still
keys leases to the host-local day (ADR 0006), settles an attempt only from an
Adept receipt the stub never returns, and lets a receipt consume a merge
permit. ADR 0006 named `OnCalendar` the sole window authority; the systemd
guide says not to add a window check to configuration. ADR 0007 adopted "the `OnCalendar` window" and
also says "per configured window"; this record refines it: the timer schedules
runs, the configuration keys leases. Issue #31 asks for a configured window and
for an open repair pull request to keep an attempt active after the host stops.

## Decision

- **Order.** Hold a cycle lock (`<state>.cycle.lock`) for the invocation,
  because `scan` rewrites the state file without the state lock. Observe recorded work; if it is unsettled, stop. Otherwise gate,
  then run `scan` as a subprocess and repair-queue `sync --apply` in process
  with the cycle's `--state`, then `_authorize` at selection, `begin`, and
  `_dispatch_host`, whose own `_authorize` is the execution-time recheck and
  whose returned binding builds the host request. No published repair or no
  approval is a valid no-op that creates no lease.
- **Settlement.** An attempt is settled when nothing was launched, when it is
  disposed, when a legacy receipt said `terminal`, or when every pull request
  the host left is merged or closed. A `completed` host run without a pull
  request, a `failed` run, a `stopped` run, or a pull request touching a file
  outside the approval fails the attempt; an `unknown` run fails and stays
  reported active. Each failure blocks new work until a disposition.
- **Window.** `window_minutes` in the cycle configuration (default 1440) sets
  the window; its key is the host-local wall-clock start floored to that size
  from 2000-01-01T00:00, so the default is the host-local day. The timer still
  decides when runs happen and still does not catch up; the configuration
  bounds how many leases a window may start. The lease stores
  `window_start`; a legacy `day_key` reads as that day's 00:00 window.
- **Merge permit.** Removed from new leases and state. Legacy `merge_permit`,
  `merge_permit_day`, and `merge_consumed` decode and are ignored; no code path
  consumes or checks them. The receipt protocol (`AdeptCycleClient`,
  `AdeptReceipt`, `_limit_failure`) is removed; a legacy receipt's `state` is
  read only for settlement and disposal.

## Consequences

A second invocation during a run exits without touching state. A pull
request closed unmerged settles the attempt and leaves its repair issue
selectable; the operator closes the issue or removes the approval to stop a
retry. A manual run or a timer run with no approved published repair does discovery
and publication and exits as a no-op. A second timer in the same window, or a
manual run after a timer run, parks `window-attempt-complete`. The observation
allowance (#28) counts consecutive observations that reached no verdict; a
successful pull-request read resets it, so a long review does not exhaust it,
but approval expiry during review still fails the attempt. The state file stays
host-writable (ADR 0015), so the settlement facts it holds are as trustworthy
as the account boundary. Running `scan` inside the cycle makes its runtime
part of the unit's run time but not of the host budget. Changing
`window_minutes` re-aligns windows from the next run.

## Considered & rejected

- **Do nothing.** judgment: fit; issue #31's outcome is the composed path.
- **Keep the host-local day.** judgment: fit; issue #31 and ADR 0007 ask for a
  configured window, and a twice-daily timer would get one repair per day.
- **Take the window from `OnCalendar` alone.** verified: `man systemd.exec`
  (systemd 259) sets `$TRIGGER_TIMER_REALTIME_USEC` only for a service a timer
  activated, so a manual run carries no window and could start a second lease
  beside the timer's.
- **Rolling window from the last lease start.** judgment: fit; a timer that
  fires a fraction of a second earlier than the previous run would be refused.
- **Settle on host exit.** judgment: fit; issue #31 requires an open repair
  pull request to keep the attempt active after the host stops.
- **Run refresh and sync as separate `ExecStartPre=` units.** judgment:
  complexity; a manual run would no longer be the same path as the timer
  (ADR 0007), and a refresh failure could not park the cycle.
- **Call `scan` in process.** verified: `cmd_scan` in
  `desloppify/app/commands/scan/cmd.py` at `212cccef` needs the CLI's runtime
  scope, resolved path, and loaded config (`desloppify/cli.py` `main`), which
  `repair-cycle`'s namespace does not carry.
