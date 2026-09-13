# Revalidate architectural concern evidence

## Problem

Confirmed imported concerns currently inherit a file-addressed work-item ID.
Renaming a reviewed file loses the link to an existing plan reference, while a
recheck can retain the same ID after its owner or protected contracts changed.
The existing dismissal record also compares source IDs rather than the concern
evidence that justified declining the work.

## Scope

For confirmed imported concerns, hash canonical JSON with schema version 1.
Identity inputs are normalized `dimension`, `identifier`, `root_cause_cluster`,
and `proposed_owner`; evidence inputs are normalized
`maintenance_consequence`, sorted unique `protected_contracts`, and
`verification`. `protected_contracts` is the sole governing-decision fixture;
its normalized-value change represents that decision change without an ADR-file
lookup or a new ADR-evidence surface. Paths, related-file paths, summary text,
and free-form evidence are excluded. Persist both hashes in concern detail. During post-scan plan
reconciliation, move an old plan reference to a new ID only when one successor
shares both hashes. The move replaces the old ID in queue, skip, override,
cluster, action-reference, and promoted-ID collections, then records the old
entry as remapped to the successor.

For changed identity or evidence, supersede the plan reference and write
`revalidation_reason: concern_evidence_changed`, whether the current concern
keeps its ID or is a successor. The current issue remains open. This durable
plan-state handoff is consumed later by the excluded promotion and execution
owners. Missing fields, failed analysis, and multiple candidates are unknown
states: they do not move a reference or write a revalidation reason.

For a dismissal, retain the existing normalized signal fingerprint through the
current import boundary. Resolve it against generated concerns and store a hash
of concern type plus sorted source-detector names, with a hash of sorted
path-independent source-finding suppression fingerprints. Exactly one stored
dismissal with both hashes suppresses the concern. Changed source evidence is
the recorded reconsideration trigger; missing or ambiguous evidence is unknown
and leaves the concern visible. Cleanup retains a dismissal whose old source IDs
are gone only when exactly one current generated concern has the same hashes.
This implements ADR 0004.

The change owns concern identity, state, reconciliation, and focused fixtures.
It excludes concern discovery/grouping (#3), GitHub queue work (#5), scheduling
or execution (#6), pilot/language validation (#7), and unrelated detector rules.
No ownership transition is needed: the concern/state boundary remains the owner
of comparison, and plan reconciliation remains the owner of reference movement.

## Failure model

- Actors and deployments: a local review import and a local scan reconcile
  persisted state and an optional plan.
- Invariants and assets at stake: a disposition must not move to unrelated work;
  a changed architectural claim must not remain eligible through stale evidence.
- Accepted failure classes: legacy or incomplete evidence is unknown because it
  cannot establish a safe comparison; existing state loading retains compatibility.
- Covered elsewhere: concern discovery/grouping is #3; GitHub promotion is #5;
  execution policy is #6; pilot validation is #7.

## Success

- A supported path rename preserves a single concern's plan reference and
  recorded disposition when its canonical identity and governing evidence match.
- A same-ID or successor changed-evidence concern removes the stale plan
  reference and records `revalidation_reason: concern_evidence_changed` for
  downstream promotion and execution owners.
- An ambiguous identity match, incomplete import, or failed analysis transfers
  neither approval nor a plan reference.
- A recorded dismissal stays declined only for one matching identity/evidence
  pair, including a supported rename, and reappears after its recorded
  reconsideration trigger changes.

## Validation

- Focused state/import tests prove identity and evidence persistence, unchanged
  rename transfer, `protected_contracts` governing-decision invalidation,
  dismissal cleanup across a rename, and dismissal reconsideration.
- Focused reconcile tests prove ambiguity and incomplete evidence leave both
  plan references and approval transfer untouched, including a same-hash item
  from a non-concern detector.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
  `make tests` remain green.
