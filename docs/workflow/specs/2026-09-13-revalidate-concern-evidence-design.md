# Revalidate architectural concern evidence

## Problem

Confirmed imported concerns currently inherit a file-addressed work-item ID.
Renaming a reviewed file loses the link to an existing plan reference, while a
recheck can retain the same ID after its owner or protected contracts changed.
The existing dismissal record also compares source IDs rather than the concern
evidence that justified declining the work.

## Scope

For confirmed imported concerns, derive a canonical identity from the stable
architectural claim and a separate digest from its governing evidence. Persist
both in the existing concern detail and dismissal record. During post-scan plan
reconciliation, move an old plan reference to a new ID only when one candidate
shares both values. When identity matches but evidence differs, retain a
revalidation marker for downstream promotion and execution owners until a fresh
review records the new evidence. Missing fields, failed analysis, and multiple
candidates are unknown states: they do not move a reference or revive a
declined concern. This implements ADR 0004.

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
- A changed sibling or governing-decision input records a stale disposition for
  downstream promotion and execution owners until review supplies replacement evidence.
- An ambiguous identity match, incomplete import, or failed analysis transfers
  neither approval nor a plan reference.
- A recorded dismissal stays declined for the same evidence and reappears only
  after its recorded reconsideration trigger changes.

## Validation

- Focused state/import tests prove identity and evidence persistence, unchanged
  rename transfer, governing-evidence invalidation, and dismissal reconsideration.
- Focused reconcile tests prove ambiguity and incomplete evidence leave both
  plan references and approval transfer untouched.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and
  `make tests` remain green.
