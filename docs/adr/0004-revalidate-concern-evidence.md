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
identity and unchanged governing evidence. Confirmed identity hashes canonical
JSON (`schema: 1`) of normalized `dimension`, `identifier`,
`root_cause_cluster`, and `proposed_owner`; evidence digest hashes canonical
JSON of normalized `maintenance_consequence`, sorted unique
`protected_contracts`, and `verification`. Canonical JSON uses sorted keys and
compact separators; paths, related-file paths, summary text, and free-form
evidence are excluded from both inputs.

A same-identity changed digest leaves the old plan entry superseded with
`revalidation_reason: concern_evidence_changed` and the observed successor as
its candidate. This is the durable handoff for downstream promotion and
execution owners; this record does not perform their work. Missing evidence or
multiple matching successors is unknown and transfers nothing.

A dismissal keeps the existing normalized signal fingerprint. Import resolves
that fingerprint to the current generated concern and stores a dismissal
identity hash of concern type plus sorted source-detector names, alongside a
digest of sorted path-independent source-finding suppression fingerprints.
Suppression applies only when exactly one stored dismissal has both values. A
changed digest is the recorded reconsideration trigger; missing or ambiguous
evidence does not suppress the generated concern.

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
