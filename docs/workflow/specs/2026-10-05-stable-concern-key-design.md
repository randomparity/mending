# Key repair-queue issues by a stable concern key

Issue: #23 (part of #17). Supersedes, in code, the identity+evidence marker of
[ADR 0005](../../adr/0005-github-repair-queue.md); its immutable successor
record belongs to #25 (ADR 0008).

## Problem

`marker_for_hashes(identity, evidence)` makes the public marker, the local
link/pending/revalidation binding, and scan-merge preservation all depend on
the evidence digest. When evidence changes, scan merge drops the link and
pending record, the next sync searches a marker no issue carries, and a second
issue is created for the same concern.

## Scope

- **Key.** `concern_key(repository, identity)` = SHA-256 of
  `desloppify-concern-key:v1\n<OWNER/REPO>\n<identity>`. The evidence digest is
  never an input. `PromotionCandidate.marker` becomes `.key`.
- **Record shape.** `github_repair` = `{key, repository, evidence_digest,
  number, url, state}`; `github_repair_pending` = `{key, repository,
  evidence_digest}`; `github_repair_revalidated` = `{key, repository,
  evidence_digest, attestation}`. `evidence_digest` is the version the record
  was written against and is not part of matching for link/pending.
- **Legacy recognition.** One engine function, `normalize_record(kind, record,
  repository, identity, evidence_digest)`, returns `None` for an absent record,
  the new-shape dict for a valid new or legacy record, and raises
  `RepairRecordError` otherwise. A legacy record (`marker`, no `key`) is valid
  only when `marker == legacy_marker(identity, evidence_digest)` for the hashes
  supplied and its repository matches; it normalizes with that
  `evidence_digest`. A new record is valid only when `key` equals the computed
  key and the repository matches. Kind-specific fields are validated (link:
  positive int number, non-empty url, state `open`/`closed`/null; revalidation:
  non-empty attestation). A record carrying both `key` and `marker`, or neither,
  is invalid.
- **Revalidation stays evidence-bound.** A candidate requires a revalidation
  that normalizes against current hashes *and* whose `evidence_digest` equals
  the current digest. Any error makes the item ineligible. Revalidate writes the
  new shape.
- **Scan merge** (`_preserve_repair_metadata`). Identity changed or either side
  lacks hashes: drop all three records (a different concern). Identity
  unchanged: normalize link and pending against the *previous* hashes and keep
  the normalized form; keep an unrecognized record verbatim so sync parks on
  it. Keep revalidation only when the evidence digest is unchanged and it
  normalizes.
- **Sync.** Eligible candidates are grouped by key; a key held by more than one
  candidate parks all of them (ambiguous identity). Per candidate:
  a `RepairRecordError` on its link or pending parks it before any GitHub call.
  Own link → read by number (unchanged). Otherwise consult *peer* records:
  `github_repair`/`github_repair_pending` on other work items (any status; a
  rename) whose own identity yields the same key. A peer pending record or a
  peer record that fails normalization parks the candidate before any GitHub
  call (`recover <key>` clears peer pending as it does today). Peer links with
  one distinct number are read by number and adopted; two distinct numbers
  park. With no peer record, search GitHub for
  the key and for the identity digest (legacy bodies print it) and union the
  results by issue number — the key term still finds an issue whose identity
  line a human removed; then the existing pending/adopt/ambiguous/create
  rules apply. Every link write stores the new shape with the current evidence
  digest and the GitHub state, so a closed (dismissed) issue stays linked and
  closed; nothing reopens or recreates it.
- **Create** keeps the lock → pending → create → re-search → locked recheck
  sequence. Under the lock it recomputes candidates and peer records and also
  refuses when the key is held by another candidate or any peer record. The body marker becomes
  `<!-- desloppify-concern-key: <key> -->`; title uses `key[:12]`.
- **Recover** clears a pending record whose `key` or legacy `marker` equals the
  argument and whose repository matches. CLI arguments are unchanged.
- **Migration** is read-time recognition plus rewrite at the next locked write
  (link write, pending write, revalidation, scan merge). Legacy shapes stay
  readable indefinitely; no bulk rewrite pass.
- Out of scope: evidence manifest (#24), recheck wiring and ADR 0008 (#25),
  host adapter (#19), briefs (#18), contract docs (#16), live publication (#7),
  independent-machine coordination, a new ticket service.

## Failure model

- Actors and deployments: one local authenticated operator running
  `repair-queue` against one repository and one shared state file, which other
  local writers (scans, the #6 repair-cycle unit) also update under the state
  lock; GitHub via the installed `gh`.
- Invariants and assets: at most one GitHub issue per key per repository among
  writers sharing a state file; links, pending attempts, and closed-issue state
  are never discarded by scan merge while both scans carry the same identity
  digest; public text holds only digests and static labels.
- Accepted failure classes:
  - Rename *and* a human deleting both marker lines with no surviving local
    link or pending record anywhere in the state file can still create a
    second issue — no durable evidence of the first remains.
  - An identity change (owner, cluster, dimension, identifier) is a new
    concern and gets a new issue, by definition of the key.
  - A scan that carries no concern hashes (e.g. re-imported unconfirmed) drops
    the records, as before this change: there is no identity to bind them to.
  - Parked items need operator action and no new command repairs them:
    ambiguous GitHub matches (e.g. duplicates the pre-#23 bug already made) →
    remove the marker and identity lines from the duplicate's body; ambiguous
    local keys → resolve one work item; corrupt records → edit the state file;
    peer or own pending → attested `recover`.
  - Mixed versions on one state file: an older release's scan merge drops
    every new-shape record. Every writer of the shared state file (including
    the timer unit) must run this version or later.
- Covered elsewhere: freshness proof and source rereads (#24), dispatch-time
  recheck (#25), cross-machine coordination (excluded; ADR 0005 boundary).

## Success

1. Same identity and repository, any evidence digest → same key; another
   repository or identity → different key.
2. After an evidence change, scan merge keeps the link (including `closed`
   state) and pending record. The item becomes syncable again only after the
   ADR 0004 transient `previous_concern_*` fields clear on a following steady
   scan and a fresh revalidation; sync then reconciles the linked issue and
   calls `create` zero times.
3. A revalidation recorded for old evidence does not make the item eligible.
4. Legacy link, pending, and revalidation records for current hashes are
   accepted and rewritten to the new shape on the next locked write; a legacy
   pending record leads to zero `create` calls until an attested `recover`
   clears it.
5. A corrupt or other-repository link/pending record (own or peer), a peer
   pending record, two candidates sharing a key, two distinct peer links for a
   key, or more than one GitHub match parks with zero `create` calls.
6. A renamed concern adopts the old item's local link, or the pre-migration
   GitHub issue found by identity digest, with zero `create` calls.
7. Existing lock, pending-before-write, uncertain-create, and attested-recover
   tests still pass with the new record shape.

## Validation

Fixture state and injected `gh` clients only, in
`desloppify/tests/repair_queue/test_promotion.py` (key, normalization, merge)
and `desloppify/tests/commands/test_repair_queue.py` (sync/create/recover).
Each Success item maps to at least one focused test named in the plan.
