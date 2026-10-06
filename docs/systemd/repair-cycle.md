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
at `/var/lib/mending/repository`, and keep that checkout current with its
default branch: the cycle scans and rechecks it as it is and never fetches.
The scan also writes its working files under `.desloppify/` in the checkout,
which the target repository should ignore; confirm it commits no
`.desloppify/` files, because a committed `.desloppify/config.json` with
`trust_plugins` makes every refresh run the checkout's plugins.
Create `/var/lib/mending/repository-worktrees` (where the Adept skills put the
repair worktree) and `/var/lib/mending/claude` (the Claude Code configuration
directory, holding the account's credentials and session files), both owned
by `mending`, the second with mode `0700`. Put the host's permission rules in
Claude Code's managed settings file, `/etc/claude-code/managed-settings.json`,
owned by root and not writable by `mending`: anything in the writable
configuration directory, the host itself could change; the unit can write only these two and the checkout, and
hides `/home`. Install the `claude` executable outside `/home`. Copy the two
unit files in this directory to `/etc/systemd/system/`.

Create `/etc/mending/repair-cycle.env`, owned by `root:mending` and mode `0640`:

```text
MENDING_REPAIR_CYCLE_CONFIG=/etc/mending/repair-cycle.json
```

Create `/etc/mending/repair-cycle.json`, owned by `root:mending` and mode
`0640`. The operator selects the repository identity, model, runtime, call
limit, USD cost cap, the host executable as an absolute path, and the Adept
plugin directory and version the host loads. Runtime and call limit may be
omitted to use their 90-minute and 100-call defaults. `window_minutes`
(default 1440, one host-local day) is the window in which at most one new
repair starts. `observation_call_limit` (default 3) and `observation_minutes`
(default 5) bound how a recorded attempt is observed; see below.

```json
{
  "enabled": true,
  "repository": "OWNER/REPOSITORY",
  "model": "SELECTED_ORCHESTRATOR_MODEL",
  "runtime_minutes": 90,
  "call_limit": 100,
  "cost_cap_usd": "25.00",
  "window_minutes": 1440,
  "host_executable": "/usr/local/bin/claude",
  "adept_skills_dir": "/opt/adept",
  "adept_skills_version": "ADEPT_PLUGIN_VERSION",
  "authority": {
    "schema": 1,
    "revision": "2026-10-06.1",
    "approvals": [
      {
        "key": "CONCERN_KEY",
        "reviewed_brief_version": "REVIEWED_BRIEF_VERSION",
        "evidence_digest": "EVIDENCE_DIGEST",
        "files": ["src/module.py", "tests/test_module.py"],
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
`evidence_digest` of its current brief. Copy them from the repair's
`github_repair` link record right after a `repair-queue sync --apply`, which
rewrites the record and prints `Changed ...` when they move; the cycle
recomputes both and refuses a stale pair. The pair is computed from work items
in the state file, which the coding host can write, so approve it only for a
brief you reviewed: the published issue shows the brief as it was created, and
a `Changed ...` line means the pair no longer matches that text. The cycle runs
`scan` and `repair-queue sync --apply` itself with its own `--state` file; do
not run either by hand against that file while the unit is active.
`files` is the whole set of paths the repair may change: the brief's evidence
manifest files and any test or documentation file the repair will touch. The
host is told this set, and a repair pull request that changes any other path
fails the attempt (`authority-scope-exceeded`). The check reads every page of
each pull request's changed files and counts the old path of a renamed file as
changed, so a rename out of an unapproved path fails too. A pull request whose
files cannot all be listed is not settled: one past the 3000 files GitHub lists,
or one too large to page through within the 10-second lookup limit, parks the
run as `pull-request-lookup-unavailable`. A pull request whose edits
cannot be compared because its approval was removed or the configuration is
unreadable fails the attempt with that authority reason instead of settling,
so remove an approval only after its attempt has settled. The approval also names the
`repair` action (the only one supported), limits at or above the cycle's own,
and an expiry; set `expires_at` past the expected review of the pull request,
because an expired approval fails an attempt whose pull request is still open.
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
When no published repair is selectable, or none has an approval, the run is a
discovery-only no-op (`Repair cycle no-op: ...`) and creates no attempt.

The configuration grants authority only when the account running the cycle
neither owns nor can write it, or any directory above it, and `--config` is
not a symbolic link or under one; otherwise every run parks as
`authority-untrusted`. The root-owned files above meet this. Never
run the cycle as root, and keep a manual pilot's configuration in a directory
owned by another account.

Each run does, in order: observe a recorded attempt that is not settled, and
stop there; refresh with `desloppify scan`; revalidate and publish with
`repair-queue sync --apply`, both bounded by the configured runtime; select and authorize one published repair; then
dispatch it once to the Claude Code host, which runs the Adept skills and stops
at a draft pull request. A second run that starts while one is running prints
`Repair cycle already running.` and exits. A failed refresh or publication
parks as `refresh-failed` or `publication-failed` before selection.

The command enforces the selected runtime while each authority or selection
call is running, and the host adapter enforces it, the call limit, and the USD
cap on the host run. An attempt stays active after the host stops while a pull
request it opened is still open; it settles once every such pull request is
merged or closed. A pull request closed unmerged also settles the attempt and
leaves its repair issue open and approved, so a later window may select it
again; close the repair issue or remove the approval to stop that. A completed
host run without a pull request (`no-pull-request`), a failed run
(`host-failed`), a stopped run (`runtime-exhausted` or `budget-exhausted`), and
a run whose worker could not be verified stopped (`dispatch-outcome-unknown`)
fail the attempt; the last stays reported active.

Observing a recorded attempt is separate from starting work: each run may spend
one observation, bounded by `observation_minutes`, even after the runtime has
expired or the configuration is disabled, and it never starts work. A run that
reads the attempt's pull requests resets the count; `observation_call_limit`
bounds consecutive observations that reached no verdict, such as an unreadable
pull request (`pull-request-lookup-unavailable`) or a worker that cannot be
verified. When it is spent, the attempt fails as `observation-exhausted`.

The state file's `attempt_failure` keeps the attempt's first failure. While it
is set, no new work starts: a settled attempt parks later runs as
`disposition-required`, and a still-active one keeps parking with its
observation reason. Review the attempt, then record a disposition:

```sh
desloppify repair-cycle --config /etc/mending/repair-cycle.json \
  --state /var/lib/mending-repair-cycle/state.json --dispose-attempt ATTEMPT_ID
```

The attempt ID is `current_lease.attempt_id` in the state file. Disposing makes
no external call and cancels nothing: it asserts the attempt has stopped or is
abandoned. If a host dispatch may still be running or have an open pull
request (`dispatch.phase` is `intent` or `unknown`, `dispatch.outcome` is
`unknown` or `completed`, or `dispatch.pull_requests` is not empty and the phase
is not `settled`), or a legacy `authoritative_receipt.state` is `active`,
disposal is refused until you confirm on the host that the attempt's worker
and pull request are finished, check that the pull request changed only the
approved files, and add `--confirm-stopped`; otherwise a second repair can run
beside it. A later window may then start new work.

Set `enabled` to `false` to park new work while retaining enough local state to
observe an already-recorded attempt. A missing model or positive USD cap also
parks before model work.

## Window and activation

Edit `OnCalendar` in `mending-repair-cycle.timer` to choose when runs happen,
and set `window_minutes` to the same cadence: the timer schedules runs, and the
configured window bounds new repairs to one per window for timer and manual
runs alike ([ADR 0016](../adr/0016-composed-repair-cycle.md)). Windows start at
consecutive blocks of `window_minutes` counted from 2000-01-01 00:00 host-local
time; choose a value that divides 1440 so they start at the same times each
day. With no timezone suffix,
systemd interprets `OnCalendar` in the host system timezone. `Persistent=false`
deliberately skips missed windows rather than catching them up. A run in a
window that already started a repair parks as `window-attempt-complete`.

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
the scheduler finds an unsettled recorded attempt, it observes that attempt
before any new selection.

## Host adapter and pilot

Repairs run through `ClaudeHostAdapter`, a Mending-owned adapter for the
Claude Code CLI that runs the installed Adept skills
([ADR 0010](../adr/0010-claude-code-host-adapter.md)); Adept is not a service.
Target opt-in, the manual pilot, and timer activation remain #7
responsibilities, and merge is outside the pilot.
