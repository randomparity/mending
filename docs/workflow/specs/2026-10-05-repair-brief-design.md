# Sanitized repair brief and public-safe rendering

Issue: #20 (part of #18). Consumes #23's stable key and #25's bound source
manifest ([ADR 0008](../../adr/0008-source-bound-repair-promotion.md)).
Decision record: [ADR 0009](../../adr/0009-sanitized-repair-brief.md),
successor to ADR 0005's public-body decision.

## Problem

`render_issue` publishes `Validated repair <key prefix>` and a body whose
ownership, contracts, and verification lines say "retained in the local
validated concern record". A worker with repository access cannot start from
the issue. There is no brief schema, no validator, and no sanitizer.

## Scope

**Brief** (`RepairBrief`, new `desloppify/engine/repair_brief.py`), schema
`desloppify-repair-brief:v1`, built by `build_brief(issue, candidate)` from
the work item and its `github_repair_revalidated` manifest:

| Brief field | Source | Required |
|---|---|---|
| `problem` | work item `summary` | yes |
| `consequence` | `detail.maintenance_consequence` | yes |
| `revision`, `manifest_digest` | revalidation manifest (complete) | yes |
| `evidence` | `detail.evidence` (list) | yes, ≥1 |
| `affected` | manifest dependencies as `(path, role)` | yes, ≥1 |
| `owner` | `detail.proposed_owner` | yes |
| `fix` | `detail.suggestion` | no; absent or blank is omitted |
| `contracts` | `detail.protected_contracts` (list) | yes, ≥1 |
| `verification` | `detail.verification` | yes |
| `confidence` | work item `confidence` ∈ {high, medium, low} | yes |

Allowed scope is `affected`; excluded scope (files outside `affected`, any
change to a protected contract) and the completion criterion (the change is
in place within the allowed scope, verification passes, every protected
contract holds) are fixed renderer text over validated fields, not source.

**Validation and sanitization** run field by field before any rendering.
Each text value must be a string; runs of whitespace collapse to one space;
the result must be non-empty and at most 1000 characters; `evidence` and
`contracts` hold at most 20 items (`affected` is bounded by the manifest's
64 dependencies and the body cap). A value is rejected when it contains,
after collapsing: a Unicode `Cc`/`Cf`/`Cs`/`Co` character (controls, bidi
overrides, lone surrogates); a secret shape (private-key header,
AWS/GitHub/Slack/Google/`sk-` tokens, JWT, quoted `password|secret|token|
api_key = "…"`); a private identifier (email, IPv4 address, a token that
`ipaddress.ip_address` parses as IPv6 and that holds a digit, `/home/`,
`/Users/`, `/root/`, `C:\Users\`, `~/`, a dotted
`*.internal|corp|lan|intranet` host); a link or destination (`scheme://`,
`//host`, `www.`, `mailto:`, `javascript:`, `data:<type>/`, a dotted host
ending in `com|net|org|io|dev|app|co|me|info|xyz|example` followed by `/`);
in `problem` only (it becomes the title), a `#N`, `owner/repo#N`, or
`@name` reference (`reference`); or a hostile instruction
(ignore/disregard previous instructions, system prompt, "you are now", new
instructions). The manifest must parse and be `complete`, else the brief is
unbound. A missing required field, any present value of the wrong type
(`confidence` included), or any rejected value — optional `fix` included —
yields `ParkedBrief(field, reason)`, whose reason is a fixed category and
never the value; only an absent or blank `fix` is omitted. A rendered body
over 60000 UTF-8 bytes parks as `too-large` (below GitHub's body limit and
Linux's per-argument limit).

**Unsupported claims.** A claim is unsupported when review prose is presented
as source-verified. Only `revision`, `affected`, and the digests are
source-bound; `evidence-digest` covers consequence, contracts, and
verification only, and nothing re-verifies `problem`, `evidence`, or `fix`
after a re-import. Control: source-bound values come only from the manifest
and candidate, never from prose, and render under `Source-bound`; every
model-authored field renders under `Reviewer assertions (not verified
against source)`.

**Rendering** (`render_brief(brief) -> (title, body)`): title `Repair: <problem>`
cut to 120 characters (plain sanitized text). Body sections: Source-bound
(revision; affected files as allowed scope; excluded scope), Reviewer
assertions (problem, consequence, evidence, proposed owner and fix,
protected contracts, required verification), Risk and uncertainty,
Completion criterion, Provenance. Every body source value is rendered inside a code span whose backtick
fence is longer than any backtick run in the value, so it renders as inert
text (no HTML, link, mention, or reference). Provenance carries #23's
`KEY_LINE` on its own line, then a `text` fence with `schema`,
`concern-key`, `concern-identity`, `evidence-digest`, `manifest-digest`,
`source-revision`, and `brief-version`. `brief.version` is the SHA-256 of the
canonical JSON of the brief record (schema, digests, every field); #22
consumes it. Wording changes alter the version, not the key, so adoption
never duplicates an issue.

**Publication** (`app/commands/repair_queue.py`): `_create_once` builds the
brief from the locked state after `_recheck_locked` and before writing
`github_repair_pending`. A parked brief prints `Parked <id>: brief field
<field> cannot be published (<reason>); hand the concern off privately.` and
writes no pending record and calls no `create`. A dry run prints `Would
park …` or `Would create …`. `GitHubIssueClient.create(repository, title,
body)` replaces `create(repository, candidate)`; `render_issue` is removed.
Pending marker, re-search after create, link readback, and #25's body-marker
adoption check are unchanged.

## Failure model

- Actors and deployments: one local operator or the #6 timer running
  Mending against its own checkout; brief values come from imported model
  output (untrusted); the published issue is public.
- Invariants and assets: no source value reaches `gh issue create` unless it
  passed its field's sanitizer; nothing is published for a parked brief; no
  rejected value is printed; the key line keeps ADR 0008 adoption working.
- Accepted failure classes:
  - Pattern sanitizing misses unenumerated secret or identifier shapes and
    paraphrased instructions; values stay inert text, and workers treat the
    issue as untrusted input (ADR 0009).
  - False positives (e.g. code quoting `token = "…"`) park a usable brief;
    the operator hands it off privately.
  - Long values park instead of being truncated.
  - A bare host under an unlisted TLD followed by a path is not rejected; in
    the body it renders as inert code-span text, and in the title as plain
    text that a reader may still follow by hand.
- Covered elsewhere: under the #6 timer a park is a log line only and
  `sync` still exits 0; durable park state and surfacing belong to selection
  (#22) and dispatch (#19). Classification and proposal briefs (#21); selection and
  brief-version invalidation (#22); dispatch (#19); live publication (#7);
  contract docs (#16).

### Threat model

- Boundary added: imported review text → public GitHub issue body/title.
- Actors: model output and whoever influenced the reviewed code; the
  operator is trusted; GitHub readers and the worker host read the issue.
- Controls: per-field type, length, character, and pattern checks; inert
  code-span rendering; fixed renderer text for scope and completion;
  fail-closed park with category-only output; `gh` keeps fixed argv.
- Out of scope: secrets with no recognizable shape; editing an already
  published issue.

## Success

1. A valid fixture brief renders every evidence, contract, owner,
   verification, and affected-path value, the seven provenance fields, and
   a body `carries_concern_marker` accepts.
2. Missing `verification` (and each other required field) parks with that
   field and `missing`; a non-string `fix` or list `confidence` parks as
   `invalid`; an absent or blank `fix` is omitted.
3. Each named rejection class parks with its category; neither the rendered output
   nor command stdout contains the payload; sync writes no pending record and
   calls `create` zero times.
4. A backtick-bearing or HTML/mention-bearing value renders inside a longer
   fence.
5. `brief.version` is stable for equal input and changes when any field
   changes; the key does not.
6. Problem, evidence, and fix render only under the reviewer-assertions
   heading; affected paths come from the manifest even when
   `detail.related_files` names other files.

## Validation

`desloppify/tests/repair_queue/test_brief.py` (1, 2, 4–6), migrated
`test_promotion.py` and fake clients in `test_sync_matrix.py` and
`desloppify/tests/commands/test_repair_queue.py` (3, sync path).
