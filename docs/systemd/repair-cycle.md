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
at `/var/lib/mending/repository`, cloned with an `origin` remote and left on
that remote's default branch. Before each refresh the cycle brings the checkout
current: it asks `origin` which branch its `HEAD` names (not the checkout's
tracking configuration), fetches that branch, and fast-forwards the checkout,
all within the configured runtime and with git hooks and `core.fsmonitor`
disabled. It does not commit, reset, stash, or switch branches, and moves
nothing on a checkout with uncommitted changes to tracked files, with an
ignored or untracked file the fast-forward would overwrite, on another
branch or a detached `HEAD`, ahead of or diverged from `origin`, or one it
cannot fetch or fast-forward; each of those parks the run as
`checkout-not-current` before the scan, and the journal says which. It also
parks rather than start the fast-forward with under 30 seconds of the runtime
left. A git call stopped at the runtime can still leave a lock file or a
partly updated tree; later runs then park as `checkout-not-current` until you
inspect and repair the checkout. The host can write the checkout's git
configuration like the rest of the checkout, and git configuration can run
commands (filter drivers, `core.sshCommand`, credential helpers,
`remote.*.uploadpack`, included files). So before any other git call the cycle
reads that configuration and parks as `checkout-not-current`, naming the key,
when the checkout's own configuration (`.git/config`, a file it includes, or
the worktree configuration) sets anything `git clone` does not write. It
accepts only `core.repositoryformatversion`, `core.filemode`, `core.bare`,
`core.logallrefupdates`, `core.ignorecase`, `core.precomposeunicode`,
`core.symlinks`, `extensions.objectformat`, `extensions.refstorage`,
`remote.origin.url`, `remote.origin.fetch`, and `branch.<name>.remote` and
`branch.<name>.merge`. The cycle's git calls also ignore the account's global
configuration (`GIT_CONFIG_GLOBAL=/dev/null`, git 2.32 or later; an older git
parks on any global setting instead), so only `/etc/gitconfig` and the cycle's
own overrides apply: put every git setting the cycle needs there, owned by root
and not writable by `mending`. The checkout's attribute files can still bind
paths to a filter driver, but only a driver `/etc/gitconfig` defines can run.
The cycle fetches from whichever `origin` URL the checkout names.
The unit needs network access to `origin`. For a private repository, give
`mending` a read-only credential git uses without asking (the cycle sets
`GIT_TERMINAL_PROMPT=0`, drops inherited `GIT_*` variables, and reads no
global git configuration, so configure it in `/etc/gitconfig`, not the
environment file), stored outside `/home`, which the unit hides; a read-only
deploy key is enough. Over SSH, also record the remote's host key in a
`known_hosts` file the account reads, because the unit cannot accept an
unknown key, and name the key and that file in `core.sshCommand` in
`/etc/gitconfig`.
The scan also writes its working files under `.desloppify/` in the checkout,
which the target repository should ignore. The refresh scan never loads the
checkout's plugins (`.desloppify/plugins/`): it sets
`DESLOPPIFY_DENY_PLUGINS=1`, which overrides both `trust_plugins` in a
committed `.desloppify/config.json` and `DESLOPPIFY_TRUST_PLUGINS`. The scan's
external language tools (for example `cargo check` or `npx eslint`) still run
in the checkout and can execute code it contains (#75).
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
`evidence_digest` of its current brief. Copy them from the published repair
issue you reviewed: its Provenance block shows the pair the issue was created
with, as `reviewed-brief-version` and `evidence-digest`. The account that
creates the issue may also be able to edit it, so use an issue body that GitHub
does not mark as edited, or whose edit history names only authors you trust.
Rewording the problem
(the issue title) moves neither value. Before approving, confirm that the pair
still matches the repair's `github_repair` link record after a
`repair-queue sync --apply`, which rewrites the record and prints `Changed ...`
when the pair moves; the cycle recomputes both and refuses a stale pair. The
record is computed from work items in the state file, which the coding host
can write, so it alone does not show what you reviewed. Compare the record
with the issue itself rather than relying on the absence of `Changed ...`: the
first link write after an uncertain create can store a different pair without
printing it. If they differ, the brief changed after the issue was published:
do not approve that pair against the published text. An issue published before
the Provenance block carried `reviewed-brief-version` shows no reviewed version,
so for it the pair can be read only from the host-writable record. The cycle runs
`scan` and `repair-queue sync --apply` itself with its own `--state` file; do
not run either by hand against that file while the unit is active.
`files` is the whole set of paths the repair may change: the brief's evidence
manifest files and any test or documentation file the repair will touch. The
host is told this set, and a repair pull request that changes any other path
fails the attempt (`authority-scope-exceeded`). The check reads every page of
each pull request's changed files and counts the old path of a renamed file as
changed, so a rename out of an unapproved path fails too. A pull request whose
files cannot all be listed is not settled: one at the 3000 files GitHub lists,
or one too large to page through within the 10-second lookup limit, parks the
run as `pull-request-lookup-unavailable`. A pull request whose edits
cannot be compared because its approval was removed or the configuration is
unreadable fails the attempt with that authority reason instead of settling,
so remove an approval only after its attempt has settled. The approval also names the
`repair` action (the only one supported), limits at or above the cycle's own,
and an expiry; set `expires_at` past the expected review of the pull request,
because an expired approval fails an attempt whose pull request is still open,
and a host still running at `expires_at` is stopped there (`authority-expired`).
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
stop there; bring the checkout current and refresh with `desloppify scan`; revalidate and publish with
`repair-queue sync --apply`, each of the three bounded by the configured runtime; select and authorize one published repair; then
dispatch it once to the Claude Code host, which runs the Adept skills and stops
at a draft pull request. A second run that starts while one is running prints
`Repair cycle already running.` and exits. A failed refresh or publication
parks as `refresh-failed` or `publication-failed` before selection; a checkout
that cannot be brought current parks as `checkout-not-current` before the scan.

The command enforces the selected runtime while each authority or selection
call is running, and the host adapter enforces it, the call limit, and the USD
cap on the host run. An attempt stays active after the host stops while a pull
request it opened is still open; it settles once every such pull request is
merged or closed. A pull request closed unmerged also settles the attempt and
leaves its repair issue open and approved, so a later window may select it
again; close the repair issue or remove the approval to stop that. A completed
host run without a pull request (`no-pull-request`), a failed run
(`host-failed`), a stopped run (`runtime-exhausted`, `authority-expired`, or `budget-exhausted`), and
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

The host is told to open its pull request from the branch
`mending/<session ID>`, derived from the attempt ID, pushed to the configured
repository. An attempt's pull requests are those whose head is that branch in
the repository itself, in any state; a fork's branch of the same name does not
count. The `Mending-Attempt: <attempt ID>` line the host puts in each body is a
display aid and never attributes a pull request, so a pull request the host
opens from any other branch is not the attempt's, and a host run that leaves
only such a pull request fails `no-pull-request`. Issues have no head branch:
an issue is recorded as the attempt's when its body has that line and its
author is the account the cycle's own `gh` is signed in as, which the host
inherits. Recorded issues are for the operator; nothing acts on them. The
configuration names no separate host account, so the lookup asks `gh` for the
signed-in account on each observation, and a token that cannot read its own
user parks the run as a lookup failure.

## Limit enforcement

What stops each limit on a repair attempt. The proof column names tests in
`desloppify/tests/commands/test_repair_cycle_e2e.py`, which run the cycle
through the production adapter and `gh` client against a scripted host and a
fake `gh`; no live host, credentials, or GitHub calls are involved (#7 owns
the live pilot).

| Limit | Claude Code host | Mending | Proof | Unsupported or unverified |
|---|---|---|---|---|
| USD cost | `--max-budget-usd` set to the lease's remaining cap; its stop is a failed run (`host-failed`) | charges the host-reported `total_cost_usd` after the run, or the whole admission when the run reports none (crash, stop, unknown); a cost over the cap fails `budget-exhausted` after the fact | `test_budget_limits_fail_the_attempt` (`host-budget`, `cost-overrun`), `test_a_crashed_host_fails_the_attempt` | Mending cannot stop a run on cost while it runs: the stream carries no running cost. Whether `--max-budget-usd` binds under the deployment's login is unverified: Claude Code 2.1.292 documents it as "Maximum dollar amount to spend on API calls (only works with --print)", and a subscription login may not be bound by it. Checking needs a paid run (#7). |
| Model calls | none; the host has no call-limit option | counts streamed assistant messages, including forwarded subagent messages, and stops the worker tree past `call_limit` (`budget-exhausted`); background tasks are disabled through `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | `test_budget_limits_fail_the_attempt` (`call-limit`) | A model process launched through a host tool, such as a nested `claude -p` from the Bash tool, does not stream into the host's output, so its calls and cost are not counted; it is still stopped with the tree (`test_a_nested_model_process_is_stopped_but_not_counted`). |
| Runtime | none | at the lease deadline, `SIGTERM` then `SIGKILL` to the host's process group and every process carrying its `MENDING_HOST_SESSION` marker (`runtime-exhausted`); a tree it cannot verify empty fails `dispatch-outcome-unknown` and stays reported active | `test_timeout_stops_a_surviving_child_and_never_relaunches`, `test_an_unverifiable_worker_stays_reported_active` | A descendant that leaves the group and clears its environment, or changes user, is not seen ([ADR 0010](../adr/0010-claude-code-host-adapter.md)). |
| Approval expiry and revocation | none | checked at selection, before dispatch, and on each observation of an active attempt; the host run ends at the earlier of the lease deadline and `expires_at` (`authority-expired`) | `test_expired_approval_never_launches`, `test_approval_expiry_stops_a_running_host`, `test_revoked_or_expired_attempt_fails_on_resume_and_never_relaunches` | A revocation does not stop a host that is already running; the next observation fails the attempt. |
| Permissions | the account's Claude Code settings and the managed settings file | passes no permission option (`--permission-mode`, `--settings`, `--dangerously-skip-permissions`) and passes `CLAUDE_CONFIG_DIR` through unchanged; a run that ends without a pull request fails `no-pull-request` | `test_approved_repair_runs_once_through_the_adapter_and_settles`, `test_permission_denied_host_fails_without_a_pull_request` | Unverified: that `/etc/claude-code/managed-settings.json` overrides the writable configuration directory, and that `ProtectHome=true` with `CLAUDE_CONFIG_DIR=/var/lib/mending/claude` leaves the host its credentials (#7). |
| Edit scope | told the approved files | after the run, compares the changed paths of each pull request opened from the attempt's branch from `gh pr view URL --json state,files` with the approval (`authority-scope-exceeded`) | `test_mismatched_results_fail_the_attempt` (`outside-approval`, `foreign-branch`, `fork-head`), `test_the_hosts_own_pull_request_needs_no_tag` | Edits are not blocked while the host runs. The `gh pr view` output shape was checked by hand against gh 2.97.0 only; renames and paths past the first 100 are not caught (above). |
| One worker, one pull request | none | the cycle lock, one lease per window, a dispatch intent persisted before launch, and a replay that observes a recorded dispatch and never relaunches it | `test_overlapping_invocation_leaves_the_running_cycle_alone`, the `test_a_cycle_killed_*` tests, `test_an_open_pull_request_holds_the_attempt_past_its_lease` | If Mending dies while its host runs outside systemd, the host keeps running past the lease deadline and the approval's expiry; later runs park `dispatch-in-flight` until it exits; the manual pilot (#7) owns that risk. Under the unit, systemd's default `KillMode=control-group` should stop the host with the service; that is unverified. A cycle killed after its lease is written but before dispatch spends its window without launching. |
