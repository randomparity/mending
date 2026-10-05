# 0009 — Publish a sanitized repair brief instead of digests only

## Status

Accepted (2026-10-05)

## Context

ADR 0005 decided that the public repair-issue body uses only the
deterministic marker, evidence digest, and static field labels, keeping owner,
contracts, verification, evidence, and summary text local. The result is an
issue no worker can act on without a hidden local narrative (#18, #20). ADR
0008 replaced the marker with the stable concern key and bound promotion to a
complete source manifest. The brief values are imported model output and are
untrusted.

## Decision

- **Brief.** A versioned `RepairBrief` (`desloppify-repair-brief:v1`) carries
  problem, maintenance consequence, source revision and manifest digest,
  reviewer evidence, affected files from the bound manifest, proposed owner,
  optional suggested fix, protected contracts, required verification, and
  review confidence. Allowed/excluded scope and the completion criterion are
  fixed renderer text over those fields.
- **Sanitize field by field, park on any rejection.** Every value is
  type-, length-, and character-checked and rejected on recognized secret,
  private-identifier, link, or hostile-instruction shapes. A missing or
  rejected field — optional ones included — parks the brief: nothing is
  published, no pending record is written, and the operator is told to hand
  the concern off privately. The output names the field and a fixed
  category, never the value.
- **Inert rendering.** Source values render only inside code spans whose
  fence outruns any backtick run in the value; affected area and revision
  come only from the manifest, never from review prose.
- **Provenance.** The body keeps ADR 0008's key line and adds a fenced block
  with schema, key, identity, evidence and manifest digests, revision, and a
  brief-version digest over the brief record. Wording changes move the
  version, never the key.

ADR 0005's adapter, pending-before-create, re-search, link readback,
explicit repository, and single-state-file rules, and ADR 0008's recheck and
verified adoption, stay in force. ADR 0005 is not edited; this record
replaces only its public-body paragraph.

## Consequences

Published issues now carry reviewer text, so sanitizer misses are public:
pattern checks cannot recognize every secret or paraphrased instruction, and
a worker host must treat the issue as untrusted input. False positives park
usable briefs. Issues created before this change keep their digest-only
bodies and are still adopted by key. The brief version is published but not
yet persisted or compared; #22 owns invalidation.

## Considered & rejected

- **Keep digest-only bodies (do nothing).** judgment: fit; #18 requires a
  brief a worker can start from.
- **Publish a degraded brief, dropping rejected fields.** judgment: fit; an
  issue missing verification or contracts is the unusable publication #18
  forbids, and silent drops hide that context was withheld.
- **Escape Markdown with backslashes instead of code spans.** verified:
  `gh api markdown` (mode `gfm`, 2026-10-05) rendered a code span holding a
  mention, `#1`, a URL, and `<b>` as one literal `<code>` element, while the
  same text outside it produced a mention link, an issue link, and an
  autolink; escaping needs per-construct reasoning for each of those.
- **Truncate long values.** judgment: fit; truncation can cut a verification
  step or contract mid-sentence while the brief still looks complete.
