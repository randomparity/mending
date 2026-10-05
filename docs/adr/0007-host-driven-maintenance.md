# 0007 — Run maintenance through a Mending-owned host adapter

## Status

Accepted (2026-10-05)

## Context

ADR 0006 assigned repair authority, leases, and receipts to an Adept-owned
executable protocol. Adept is installed workflow and skill content that a
coding host runs; it exposes no such service. `repair-cycle` therefore has only
the `AdeptCycleClient` protocol and an unavailable stub. ADR 0005's consequences
left claims, scope authorization, scheduling, and execution to "Adept/#6". The
README still teaches a score-maximizing, queue-draining loop.

## Decision

Ownership:

- Mending owns scan history, concern identity, dismissals, source-bound
  revalidation, candidate selection, and durable repair-attempt state.
- A Mending-owned adapter launches one concrete coding host. The host runs the
  installed Adept skills for approved scope, claims, worktree isolation,
  implementation, verification, and review.
- GitHub holds actionable issues, human decisions, and PR links. Mending
  reconciles them with its local history.
- The existing systemd timer invokes the same one-shot path, only after a
  manual pilot and explicit operator enablement.

A run has two output paths. A small repair needs current evidence, narrow
scope, a verification plan, and execution authority; it ends in a verified,
reviewed draft PR. An architecture proposal carries evidence, alternatives, and
the human decision needed; it is never dispatched, and approval leads only to a
separately scoped, revalidated execution decision. A no-op is a successful
result. Scores are optional diagnostics: no finding quotas, no score targets,
and no score-based reason to defer required tests.

Discovery-only, publication, repair execution, and merge are separately
controlled capabilities, each enabled by explicit operator configuration for
the repository. Missing opt-in, or a host that cannot enforce the configured
budgets, parks that action. A workflow instruction, issue body, model finding,
or nonempty proof string grants no authority.

ADR 0006's repository lock, persist-before-dispatch, durable correlation,
fail-closed parking, `OnCalendar` window, budgets, and no catch-up remain.
The first implementation allows one active repair and at most one newly
dispatched repair per configured window. Merge is not part of a repair attempt
or the initial pilot; any merge needs separate authorization.

This supersedes, and only these: ADR 0006's Adept-owned authority verifier,
lease/receipt protocol, and per-window merge permit; and ADR 0005's assignment
of claims, scope authorization, scheduling, and execution to Adept/#6.

Code seams and owners: the `AdeptCycleClient` seam in
`desloppify/app/commands/repair_cycle.py` becomes one concrete host adapter and
loader, keeping existing state readable and unresolved leases intact (#19).
`repair_queue.py` identity markers and revalidation gain a stable key and
source-bound evidence (#17). `render_issue` and concern-only eligibility gain
actionable briefs and the proposal path (#18). Target opt-in, the live pilot,
and timer activation belong to #7. The inherited analyzer CLI stays compatible.

## Consequences

Until #19 lands, `repair-cycle` parks before selection; no live repair path
exists. There is no Adept server, daemon, scheduler abstraction, or host
registry. ADRs 0005 and 0006 carry no supersession banner because most of each
still governs; readers find the superseded portions here. #17 and #18 record
their ADR 0004/0005 changes in their own successor records.

## Considered & rejected

- **Keep the Adept-owned protocol.** verified: `rg -n "class .*AdeptCycleClient"
  desloppify/app/commands/repair_cycle.py` at `0377143` finds only the protocol
  and `_UnavailableAdeptCycleClient`; nothing implements it.
- **Build an Adept server or daemon.** judgment: cost; it re-hosts content the
  coding host already runs.
- **Banner ADRs 0005 and 0006 as superseded.** judgment: fit; the banner marks
  a whole record non-governing, while their locks, timer, and promotion
  decisions still govern.
- **Support several hosts behind a registry.** judgment: complexity; one host
  is enough for the first pilot.
- **Keep a merge permit per window.** judgment: fit; the pilot ends at a draft
  PR and merge needs its own authority.
