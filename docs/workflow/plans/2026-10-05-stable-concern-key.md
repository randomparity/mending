# Plan: stable repository-scoped concern key (#23)

Spec: [2026-10-05-stable-concern-key-design.md](../specs/2026-10-05-stable-concern-key-design.md)

**Goal:** key repair-queue records and public markers by `concern_key(repository,
identity)` so evidence changes never orphan a GitHub issue, while legacy records
and issues are adopted and corrupt or ambiguous state parks.

**Architecture:** the engine module owns key derivation and the one record
normalizer (new + legacy shapes). Scan merge and the `repair-queue` command
consume only that normalizer; neither compares markers itself any more.

**Tech stack:** Python 3.11+, pytest, existing `uv`/`make` guardrails.

Expected implementation size: 350–450 changed lines (M) — ~170 production lines
across three modules plus ~230 test lines for the seven Success items; tests
dominate.

## Global Constraints

- No new dependency. `gh` argv shapes, `repair-queue` CLI arguments, the state
  lock, and the pending-before-write ordering stay unchanged.
- Public issue text holds only digests and static labels.
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`,
  `.github/scripts/check-records.sh`. Focused loop:
  `uv run --locked pytest -q desloppify/tests/repair_queue desloppify/tests/commands/test_repair_queue.py`.
- Do not edit `docs/adr/0005-*.md` (successor record is #25's).

## File map

| File | Owns after change |
|---|---|
| `desloppify/engine/repair_queue.py` | key, legacy marker, `concern_hashes`, `normalize_record`, `RepairRecordError`, candidate, body, `gh` client |
| `desloppify/engine/_state/merge_issues.py` | `_preserve_repair_metadata` delegates to `normalize_record` |
| `desloppify/app/commands/repair_queue.py` | sync grouping, local-link index, union search, create/link/revalidate/recover writes in new shape |
| `desloppify/tests/repair_queue/test_promotion.py` | engine + merge tests |
| `desloppify/tests/commands/test_repair_queue.py` | command tests |

`marker_for_hashes` is removed (callers: engine, merge, two test files);
`legacy_marker` replaces it for recognition only. `PromotionCandidate.marker`
becomes `.key`.

## Task 1 — Engine key and record normalizer

**Interfaces (produced):**

```python
KEY_SCHEMA = "desloppify-concern-key:v1"
class RepairRecordError(ValueError): ...
@dataclass(frozen=True)
class PromotionCandidate: issue_id: str; identity: str; evidence_digest: str; key: str; repository: str
def concern_key(repository: str, identity: str) -> str
def legacy_marker(identity: str, evidence_digest: str) -> str
def concern_hashes(detail: Mapping[str, Any]) -> tuple[str, str] | None
def normalize_record(kind: str, record: object, repository: str, identity: str, evidence_digest: str) -> dict[str, Any] | None
def matching_record(detail: Mapping[str, Any], kind: str, candidate: PromotionCandidate) -> dict[str, Any] | None  # raises RepairRecordError
def candidate_from_issue(issue, repository, *, require_revalidation=True) -> PromotionCandidate | None
def render_issue(candidate) -> tuple[str, str]
```

**Verification:**

- Key stability — `focused-test`: `test_concern_key_ignores_evidence_and_scopes_repository`;
  red: `ImportError: concern_key`; green: focused loop command.
- Normalizer — `focused-test`: `test_normalize_accepts_new_and_legacy_shapes`,
  `test_normalize_rejects_corrupt_records` (parametrized: other repository,
  wrong key, wrong legacy marker, both `key`+`marker`, neither, bool/zero
  number, empty url, state `"merged"`, empty attestation, non-mapping);
  red: `ImportError: normalize_record`.
- Evidence-bound revalidation — `focused-test`:
  `test_candidate_rejects_revalidation_for_old_evidence`; red: candidate returned.
- Body — `focused-test`: existing `test_rendered_issue_never_contains_source_text`
  asserts `<!-- desloppify-concern-key: {key} -->` in body and `key[:12]` in title.

**Steps:**

1. Write the tests above; `_issue()` fixture writes a new-shape revalidation
   `{key, repository, evidence_digest, attestation}`; keep one legacy-shape
   variant (`marker: legacy_marker(a, b)`). Run focused loop → red.
2. Implement in `desloppify/engine/repair_queue.py`:

```python
def concern_key(repository: str, identity: str) -> str:
    """Return the stable repository-scoped key; evidence is never an input."""
    return sha256(f"{KEY_SCHEMA}\n{repository}\n{identity}".encode()).hexdigest()

def legacy_marker(identity: str, evidence_digest: str) -> str:
    """Return the pre-#23 identity+evidence marker, recognized only for migration."""
    return sha256(f"{identity}\n{evidence_digest}".encode()).hexdigest()

def concern_hashes(detail: Mapping[str, Any]) -> tuple[str, str] | None:
    identity = detail.get("concern_identity")
    evidence_digest = detail.get("concern_evidence_digest")
    if _is_digest(identity) and _is_digest(evidence_digest):
        return identity, evidence_digest
    return None

def normalize_record(kind, record, repository, identity, evidence_digest):
    if record is None:
        return None
    if not isinstance(record, Mapping) or record.get("repository") != repository:
        raise RepairRecordError(f"{kind} is not bound to {repository}")
    key = concern_key(repository, identity)
    if ("key" in record) == ("marker" in record):
        raise RepairRecordError(f"{kind} has no single key")
    if "key" in record:
        version = record.get("evidence_digest")
        if record["key"] != key or not _is_digest(version):
            raise RepairRecordError(f"{kind} key does not match")
    elif record["marker"] == legacy_marker(identity, evidence_digest):
        version = evidence_digest
    else:
        raise RepairRecordError(f"{kind} legacy marker does not match")
    normalized = {"key": key, "repository": repository, "evidence_digest": version}
    normalized.update(_kind_fields(kind, record))  # raises RepairRecordError
    return normalized
```

   `_kind_fields`: `github_repair` → `number` (int, not bool, ≥1), `url`
   (non-empty str), `state` (`"open"`, `"closed"`, or `None`);
   `github_repair_revalidated` → `attestation` (str with non-blank content);
   `github_repair_pending` → `{}`; any other kind → `RepairRecordError`.
   `candidate_from_issue` uses `concern_hashes`, keeps the `previous_concern_*`
   rejection, builds the candidate with `concern_key`, and for revalidation
   requires `normalize_record(...)` to succeed, be non-`None`, and carry
   `evidence_digest == candidate.evidence_digest`; any `RepairRecordError` →
   `None`. `matching_record` = `normalize_record(kind, detail.get(kind),
   candidate.repository, candidate.identity, candidate.evidence_digest)`.
   `render_issue` swaps marker for key; delete `marker_for_hashes`,
   `_has_matching_record`; update `__all__`.
3. Focused loop → green; `make typecheck` → exit 0. Commit
   `feat: derive repair-queue key from repository and identity`.

## Task 2 — Scan merge keeps records across evidence changes

**Interfaces:** consumes `concern_hashes`, `normalize_record`, `RepairRecordError`.

**Verification:**

- `focused-test` in `test_promotion.py` (replacing
  `test_scan_merge_preserves_only_matching_repair_metadata`):
  `test_scan_merge_keeps_link_and_pending_when_evidence_changes` (closed link
  and pending survive with original `evidence_digest`; revalidation dropped),
  `test_scan_merge_drops_records_when_identity_changes`,
  `test_scan_merge_migrates_legacy_link`, `test_scan_merge_keeps_corrupt_link_verbatim`.
  Red: link absent after `upsert_issues`.

**Steps:**

1. Write tests using `upsert_issues(existing, [incoming], [], now, lang=None)`
   as the current test does. Run → red.
2. Replace `_preserve_repair_metadata` in `merge_issues.py`:

```python
def _preserve_repair_metadata(previous_detail: object, detail: dict) -> None:
    """Keep queue records while concern identity is unchanged; evidence may move."""
    if not isinstance(previous_detail, Mapping):
        return
    previous, current = concern_hashes(previous_detail), concern_hashes(detail)
    if previous is None or current is None or previous[0] != current[0]:
        return
    for kind in _REPAIR_RECORD_KINDS:
        record = previous_detail.get(kind)
        if record is None:
            continue
        repository = record.get("repository") if isinstance(record, Mapping) else None
        try:
            normalized = normalize_record(kind, record, repository, *previous) if isinstance(repository, str) else None
        except RepairRecordError:
            normalized = None
        if kind == "github_repair_revalidated":
            if normalized is not None and previous == current:
                detail[kind] = normalized
        else:
            detail[kind] = normalized if normalized is not None else copy.deepcopy(record)
```

   `_REPAIR_RECORD_KINDS = ("github_repair", "github_repair_pending",
   "github_repair_revalidated")`; import `concern_hashes`, `normalize_record`,
   `RepairRecordError` instead of `marker_for_hashes`.
3. Focused loop and `make arch` → green. Commit
   `fix: keep repair links across concern evidence changes`.

## Task 3 — Command: grouping, local links, union search, new-shape writes

**Interfaces:** consumes all Task 1 names. Adds private helpers
`_candidates(state, repository) -> list[PromotionCandidate]`,
`_local_links(state, repository) -> dict[str, dict[str, dict]]` (key → issue id →
normalized link; items whose link raises are skipped),
`_key_claimed_elsewhere(state, candidate) -> bool`.

**Verification** (`focused-test`, `test_repair_queue.py`; each red before step 2):

- `test_evidence_change_reconciles_closed_link_without_create` (dismiss/repeat): link
  `state: closed`, new evidence + fresh revalidation; `view` called, `create` 0,
  link rewritten with new `evidence_digest`, still `closed`.
- `test_old_evidence_revalidation_is_not_eligible` (reconsider): no search, no create.
- `test_legacy_link_is_migrated_on_sync` / `test_legacy_pending_never_creates_again`.
- `test_unrecognized_record_parks_before_github` (parametrized: link for other
  repository, pending with foreign key): 0 searches, 0 creates.
- `test_ambiguous_key_parks_all_candidates` (two items, same identity).
- `test_rename_adopts_local_link_from_old_item` (old item `status: fixed` holds link).
- `test_rename_adopts_legacy_issue_found_by_identity` (search returns the issue
  only for the identity term).
- `test_conflicting_local_links_park`, `test_multiple_github_matches_park`.
- `test_human_edited_issue_reconciles_by_number` (search would return `[]`).
- Existing tests updated: searches assert `[(REPO, key), (REPO, IDENTITY)]`;
  pending equals `{key, repository, evidence_digest}`; `recover` passes the key;
  add `test_recover_accepts_legacy_marker`.

**Steps:**

1. Write tests; fake clients record `searches`, `views`, `create_calls`. Run → red.
2. In `desloppify/app/commands/repair_queue.py`:
   - `_sync`: `candidates = _candidates(state, repository)`; count keys; a key
     with count > 1 prints `Skipped {id}: concern key is ambiguous across work
     items.` for each. Build `links = _local_links(state, repository)` once.
   - Per candidate: `matching_record` for link and pending inside
     `try/except RepairRecordError` → `Skipped {id}: repair record is
     unrecognized; reconcile it manually.` Own link → `_read_link(...,
     expected_key="github_repair")`. Else peer links (other issue ids in
     `links[key]`) by distinct number: >1 → `Skipped {id}: local links for this
     concern key conflict.`; 1 → `_read_link(..., expected_key=None)`. Else the
     existing search/pending/adopt/create flow.
   - `_search`: `client.search(repo, key)` then `client.search(repo, identity)`;
     return `list({i.number: i for i in found}.values())`; any
     `RuntimeError`/`ValueError` → `None`.
   - `_create_once`: inside the lock, treat `RepairRecordError` or
     `_key_claimed_elsewhere` as "changed"; pending =
     `{"key", "repository", "evidence_digest"}`.
   - `_write_link`: expected record check catches `RepairRecordError` as stale;
     link = `{"key", "repository", "evidence_digest", "number", "url", "state"}`.
   - `_revalidate`: writes `{"key", "repository", "evidence_digest", "attestation"}`.
   - `_recover`: matches `pending.get("key") == args.marker or
     pending.get("marker") == args.marker`, repository equal.
3. Focused loop → green; then `make lint typecheck arch ci-contracts tests`
   → exit 0. Commit `feat: reconcile repair issues under the stable concern key`.

## Rollback

Each task is one commit; revert in reverse order. The previous release reads
new-shape records as absent and searches only legacy markers, so running it
after an `--apply` sync can create a duplicate issue. Pause `--apply` syncs
before rolling back past this change; dry runs are safe.
