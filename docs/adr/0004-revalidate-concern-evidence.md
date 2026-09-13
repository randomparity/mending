# 0004 — Revalidate architectural concerns by semantic evidence

## Status

Accepted (2026-09-13)

## Context

Confirmed architecture concerns are persisted as file-addressed work items.
Their IDs change when a reviewed file is renamed, while a plan may still refer
to the prior ID. Conversely, an unchanged identifier does not prove that the
concern's governing owner, contracts, or verification evidence still supports
the earlier disposition.

## Decision

Store a canonical, path-independent identity and a separate digest of the
governing concern evidence on confirmed imported concerns. Reconciliation may
transfer a plan reference only for one old and one new concern with the same
identity and unchanged governing evidence. A changed digest records a stale
disposition for downstream promotion and execution owners to revalidate; a
missing or ambiguous comparison is unknown and transfers nothing. Dismissals retain their recorded
reconsideration evidence, so they remain declined until that evidence changes.

## Consequences

Supported renames preserve one unambiguous concern's identity without treating
path text as architectural evidence. Existing state without the new evidence
remains readable but cannot prove equivalence, so it follows the conservative
unknown path. The rule is limited to local state and plan reconciliation; it
does not create GitHub work or schedule execution.

## Considered & rejected

- **Continue comparing file-addressed IDs.** verified: `make_issue` constructs
  IDs from detector, file, and name, so a path rename changes the stored key.
- **Transfer the nearest same-detector concern.** judgment: similarity without
  a unique semantic identity could silently carry an approval to unrelated work.
- **Use a single digest for identity and freshness.** judgment: a rename must
  preserve identity while a governing-evidence change must invalidate freshness.
