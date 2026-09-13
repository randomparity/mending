# Bounded architecture concerns

## Problem

Existing holistic review batches can carry detector signals, but a confirmed signal is
stored like a general review issue. The batch prompt does not require the ownership and
verification evidence needed to distinguish an architectural concern from a line-level fix,
and the existing collector output is not carried as a bounded batch reading set.

## Scope

Extend the existing confirmed-concern payload through prompt rendering, batch normalization,
and holistic import. A confirmed concern retains `concern_verdict: "confirmed"` end to end and
requires a root-cause cluster, maintenance consequence, proposed owner, protected contracts,
and bounded verification in addition to the existing files and source evidence. A missing
confirmed-only field skips that result while retaining other valid batch results; malformed
ordinary review entries keep the existing batch-rejection behavior. Dismissed signals and
ordinary review issues retain their current contracts. The existing batch cap remains the bound;
it is not tied to a score, and empty `issues` remains valid. Batch preparation will expose at
most `review_batch_max_files` paths (default 80): dimension/concern signal paths first, their
direct dependency-graph neighbors next when available, then a lexically ordered rotating slice
of other allowed production paths starting at `scan_count % remaining_count`. Prompts render the
result as an investigation reading set. The rotation is packet preparation only; it writes no
state and does not own freshness or scheduling.

## Failure model

- Actors and deployments: a local review runner emits JSON that the local import path reads.
- Invariants and assets at stake: confirmed concerns retain evidence and never masquerade as
  validated GitHub work; ordinary-review and dismissed-signal compatibility stays intact.
- Accepted failure classes: a malformed confirmed concern is skipped because its evidence is
  incomplete; broader selection, freshness, queue writes, and scheduling belong to #4–#6.
- Covered elsewhere: #4 owns revalidation and disposition; #5 owns GitHub promotion; #6 owns
  scheduled execution; #7 owns pilot and language validation.

### AI-SPEC

A batch reviewer, triggered by `review --run-batches`, reads the supplied blind packet and
returns JSON. It may use only packet and repository evidence, must emit no concern when the
required evidence is absent, and falls back to an empty array for clean code. The output is
bounded by the existing batch cap; success is a normalized confirmed concern with each required
field, while malformed output remains non-actionable.

### Failure-mode map

- Structured output: schema compliance and field accuracy are severity 4 because incomplete
  concern evidence can mislead later repair selection.
- Content generation: unsupported ownership claims are severity 4 because they can promote an
  unfounded repair proposal; the output must instead be omitted.

### Eval cases

- `confirmed-complete`: a confirmed concern with every required field normalizes and persists.
- `confirmed-incomplete`: a confirmed concern missing ownership evidence is skipped without
  discarding a valid ordinary result in the same batch.
- `ordinary-compatible`: an ordinary review issue still normalizes without concern-only fields.
- `dismissed-compatible`: a dismissed concern retains only its fingerprint contract.
- `selection-clean`: an empty signal set produces a bounded rotating reading set.
- `selection-drifted`: a signal seed and its direct neighbor precede the rotating fallback and
  never exceed the configured bound.

## Success

- A confirmed architecture concern produced by an existing batch retains its confirmed marker and
  names one root-cause cluster, concrete evidence, the maintenance consequence, proposed owner,
  protected contracts, and a bounded verification strategy.
- The batch prompt permits zero concerns and never requires an issue because of an assessment.
- A batch exposes a bounded reading set that prioritizes current signal paths and direct neighbors
  before a deterministic rotating broader sample of allowed production files.
- Existing ordinary and dismissed review payloads remain accepted by their current contracts.

## Validation

- Focused tests exercise prompt requirements and the complete, incomplete, ordinary, and
  dismissed normalization cases in the review batch test modules.
- `make lint`, `make typecheck`, `make arch`, `make ci-contracts`, and `make tests` remain green.
