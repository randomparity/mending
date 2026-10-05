# Plan: bounded evidence check during repair-queue revalidation (#41)

Goal: run a bounded evidence-anchor check in `repair-queue revalidate`, persist
its record beside the manifest, and treat a failed, unknown, or changed outcome
as not current in the sync recheck, per
[the spec](../specs/2026-10-05-repair-evidence-check-design.md) and
[ADR 0012](../../adr/0012-bounded-evidence-check.md).

Architecture: a new engine module owns parsing, bounds, outcome, and the record
digest (`repair_check.py`); `repair_manifest.py` gains a bounded blob read
through its existing `git` wrapper; `repair_queue.py` validates the extended
record; the command module only calls the runner and compares digests.

Tech stack: Python 3.11+, pytest, the `git` CLI via fixed argv.

Expected implementation size: 600–700 changed lines (M) — about 350 production
lines (new runner module ~245 with its dataclasses, record, bounds, parsing and
reads; blob read ~20; record validation ~15; command ~80; README 5) and about
290 test lines (runner cases ~170, migrated record fixtures, injected-result
command tests, sync-matrix cases). Corrected after the build: the first
estimate (330–420) undercounted the runner module and its tests; no work was
added beyond the reviewed design.

## Global Constraints

- No new dependencies. Engine modules must not import `desloppify.app`.
- Fail closed: anything but outcome `pass` with a matching digest is
  ineligible. Reasons are fixed strings; never echo evidence text.
- Do not touch `repair_cycle*.py`, `engine/repair_cycle.py`, `update_skill`, or
  skill docs (sibling campaign work).
- Guardrails: `make lint`, `make typecheck`, `make arch`, `make ci-contracts`,
  `make tests`, `make tests-full`, `make package-smoke`, and
  `BASE_SHA=$(git rev-parse origin/main) RECORD_PROFILES=adr ./.github/scripts/check-records.sh`.

## File map

| File | Owns after the change |
|---|---|
| `desloppify/engine/repair_check.py` (new) | evidence parsing, bounds, outcome, check record and digest, `passing_check` |
| `desloppify/engine/repair_manifest.py` | adds `blob_size`, `read_blob`; `_git` gains optional `timeout` |
| `desloppify/engine/repair_queue.py` | `_kind_fields("github_repair_revalidated")` requires a passing check and returns `check`, `check_digest` |
| `desloppify/app/commands/repair_queue.py` | `_revalidate` runs the check and clears on refusal; `_comparison` returns `_Recheck`; `_recheck_locked` keeps on transient unknown, clears on any other mismatch; `_check` injection seam |
| `README.md` | repair-queue paragraph names the check |
| tests | new `tests/repair_queue/test_check.py`; fixtures in `test_promotion.py`, `test_brief.py`, `commands/test_repair_queue.py` gain `check`/`check_digest`; `test_sync_matrix.py` gains cases |

No ownership transition: a clean extension of the #24/#25 owners.

## Task 1 — runner and blob read

Files: create `desloppify/engine/repair_check.py`, modify
`desloppify/engine/repair_manifest.py`, test
`desloppify/tests/repair_queue/test_check.py`.

Interfaces (later tasks rely on these exact names):
- `blob_size(root: Path, object_id: str, timeout: float) -> int | None`
- `read_blob(root: Path, object_id: str, timeout: float) -> str | None`
- `CheckResult(outcome: str, reason: str, claims: tuple[Claim, ...], transient: bool = False)`
  with `.as_record() -> dict[str, Any]` and `.digest -> str`
- `check_concern(root: Path, manifest: SourceManifest, issue: Mapping[str, Any]) -> CheckResult`
- `passing_check(record: object, digest: object) -> bool`
- constants `CHECK_SCHEMA`, `MAX_EVIDENCE_ITEMS = 32`, `MAX_EVIDENCE_BYTES = 16384`,
  `MAX_CITATIONS = MAX_DEPENDENCIES`, `MAX_FILE_BYTES = 1 << 20`,
  `MAX_TOTAL_BYTES = 8 << 20`, `MAX_SECONDS = 10`

Verification:
- Contract: outcomes per spec (pass, fail line/quote, unknown bounds/unsupported/outside).
  Mode: focused-test. Cases in `test_check.py` over a `git init` fixture under
  `tmp_path` (reuse the `_git`/`_commit` helpers pattern of `test_sync_matrix.py`):
  `test_cited_line_passes`, `test_line_past_end_fails`, `test_absent_quote_fails`,
  `test_backticked_citation_and_path_are_not_quotes`,
  `test_no_citation_is_unknown`, `test_unresolved_token_is_ignored` (`v1.2:3`
  beside a resolved citation passes; alone is unknown),
  `test_blob_over_bound_is_unknown` (monkeypatch `MAX_FILE_BYTES` to 4,
  `transient` false), `test_too_many_items_is_unknown`,
  `test_too_many_citations_is_unknown`, `test_deadline_is_transient_unknown`
  (monkeypatch `MAX_SECONDS` to 0), `test_suffix_resolution_and_ambiguity`,
  `test_digest_ignores_reason_and_bounds`,
  `test_passing_check_requires_digest_and_pass`.
  Red: `ModuleNotFoundError: desloppify.engine.repair_check`. Green:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_check.py`.

Steps:
1. Write the tests above; run; expect the import error.
2. In `repair_manifest.py`, give `_git` a keyword `timeout: float =
   _GIT_TIMEOUT_SECONDS` passed to `subprocess.run`, and add (and export):

```python
def blob_size(root: Path, object_id: str, timeout: float) -> int | None:
    """Return a blob's size in bytes, or ``None`` when it cannot be read."""
    size = _git(root, "cat-file", "-s", object_id, timeout=timeout) if _is_object_id(object_id) else None
    return int(size) if size is not None and size.strip().isdigit() else None


def read_blob(root: Path, object_id: str, timeout: float) -> str | None:
    """Return a blob's text (universal newlines), or ``None`` when it cannot be read."""
    return _git(root, "cat-file", "blob", object_id, timeout=timeout) if _is_object_id(object_id) else None
```

3. Create `repair_check.py`: `_CITATION =
   re.compile(r"(?<![\w./-])((?:[\w.-]+/)*[\w.-]*\w\.\w+):(\d{1,7})(?:-(\d{1,7}))?(?!\d)")`,
   `_QUOTE = re.compile(r"`([^`\n]{1,200})`")`,
   `_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")`; frozen dataclasses
   `Citation(path, start, end)` and `Claim(citations, identifiers)`; `_bounds()`
   returns the six constants (read at record time). `_resolve(token, present)`
   returns the exact present path, else the single present path ending in
   `"/" + token`, else `None`. `_claims(issue, manifest) -> tuple[Claim, ...] |
   str` ignores unresolved citation tokens, skips backtick spans that contain a
   citation token or `/` or resolve as a path, collects identifiers of the
   rest, and returns an input-derived unknown reason for: evidence not a list
   of strings, over items/bytes, no resolved citation, over `MAX_CITATIONS`.
   `check_concern` sets a `time.monotonic()` deadline; for each distinct cited
   path it passes `max(deadline - now, 0)` as the timeout (≤ 0 → transient
   unknown `time bound exceeded`) to `blob_size` (`None` → transient `source
   file could not be read`; over `MAX_FILE_BYTES` or running total over
   `MAX_TOTAL_BYTES` → input-derived unknown) and `read_blob` (`None` →
   transient). Line count = `text.count("\n") + (not text.endswith("\n"))`
   for non-empty text, else 0. Then `fail` with reason `cited line is outside
   the file` or `quoted identifier is absent from the cited files` (whole word,
   `re.search(rf"\b{re.escape(name)}\b", text)`), else `pass` with reason
   `evidence anchors hold`. `as_record()` = `{"schema", "outcome", "reason",
   "transient", "claims": [{"citations": [{"path","start","end"}],
   "identifiers": [...]}], "bounds"}`; `digest` = SHA-256 of `json.dumps({k:
   record[k] for k in ("schema", "outcome", "claims")}, sort_keys=True,
   separators=(",", ":"))`, shared with `passing_check` through one
   `_digest(record)` helper. `passing_check` returns false for a non-mapping, a
   wrong schema, outcome other than `pass`, a missing key or non-serializable
   record (`KeyError`, `TypeError`, `ValueError`), or a digest mismatch.
4. Run the focused command; expect all pass. Commit
   `feat(repair-check): add bounded evidence-anchor check`.

## Task 2 — record shape and command wiring

Files: modify `desloppify/engine/repair_queue.py`,
`desloppify/app/commands/repair_queue.py`, `README.md`; tests in
`desloppify/tests/repair_queue/test_promotion.py`, `test_brief.py`,
`test_sync_matrix.py`, `desloppify/tests/commands/test_repair_queue.py`.

Interfaces: consumes Task 1's `CheckResult`, `check_concern`, `passing_check`.
Adds `args.check: Callable[[Mapping, SourceManifest], CheckResult]` (tests only).

Verification:
- Contract: eligible record requires a passing check. Mode: focused-test.
  `test_promotion.py`: records without `check`, with outcome `fail`, or with a
  wrong `check_digest` are not candidates; a valid one normalizes to seven
  fields and survives `upsert_issues` rescan. Red: the record without `check`
  is still a candidate. Green:
  `uv run --locked pytest -q desloppify/tests/repair_queue/test_promotion.py`.
- Contract: revalidate stores the check or refuses. Mode: focused-test.
  `commands/test_repair_queue.py`: injected `pass` stores `check` and
  `check_digest`; injected `fail` and `unknown` raise `CommandError` and leave
  no `github_repair_revalidated` (including one stored before). Red: no `check`
  key stored. Green:
  `uv run --locked pytest -q desloppify/tests/commands/test_repair_queue.py`.
- Contract: recheck invalidates on changed outcome with unchanged source.
  Mode: focused-test. Same file: unchanged manifest + injected different
  passing result → cleared, reason `check-changed`, no search; injected `fail`
  → cleared `check-failed`; injected input-derived `unknown` → cleared
  `check-unknown`; injected transient `unknown` → skipped, record kept, no
  search. `test_sync_matrix.py` (real check, source unchanged after
  revalidate): edit `detail.evidence` to cite a line past the end → sync
  clears and creates nothing; edit it to quote an identifier absent from
  `src/impl.py` → cleared; edit it to drop the citation → cleared. Red: create
  proceeds. Green: `uv run --locked pytest -q desloppify/tests/repair_queue
  desloppify/tests/commands/test_repair_queue.py`.
- Contract: README wording. Mode: task-test-not-applicable — prose with no
  executable consumer.

Steps:
1. Migrate fixtures: every test `REVALIDATED`-style record gains
   `"check": PASS.as_record(), "check_digest": PASS.digest` where
   `PASS = CheckResult("pass", "evidence anchors hold", ())`; the commands
   test `_args` default gains `"check": lambda issue, manifest: PASS`. Add the
   new cases; run; expect the red failures above.
2. `repair_queue._kind_fields` revalidated branch: also require
   `passing_check(record.get("check"), record.get("check_digest"))`; return
   `{"manifest", "manifest_digest", "check": dict(record["check"]),
   "check_digest": record["check_digest"]}`; error message
   `github_repair_revalidated has no complete bound manifest and passing check`.
3. Command: add

```python
def _check(args: argparse.Namespace, issue: Mapping[str, Any], manifest: SourceManifest) -> CheckResult:
    supplied = getattr(args, "check", None)
    if supplied is not None:
        return supplied(issue, manifest)
    return check_concern(_source(args).root, manifest, issue)
```

   In `_revalidate`, after the manifest guard: `check = _check(args, issue,
   manifest)`; if `check.outcome != "pass"`, pop `github_repair_revalidated`,
   leave the lock normally (so the removal is saved; `state_lock` saves only
   on clean exit), then raise `CommandError(f"verification check did not pass
   ({check.outcome}: {check.reason}); the concern evidence must cite recorded
   files as PATH:LINE whose lines and quoted identifiers still hold")`;
   otherwise store `"check": check.as_record(), "check_digest": check.digest`.
   Replace `ManifestComparison` use with a frozen `_Recheck(current: bool,
   reason: str, keep: bool)`: manifest not current → `_Recheck(False,
   comparison.reason, comparison.current_digest is None)`; transient check →
   `_Recheck(False, check.reason, True)`; digest differs → `_Recheck(False,
   {"fail": "check-failed", "unknown": "check-unknown"}.get(check.outcome,
   "check-changed"), False)`; else current. `_recheck_locked` prints `Skipped <id>: source or its check
   could not be read (<reason>); revalidation kept.` when `keep`, else the
   existing clear message with the reason.
4. README: append to the "Available today" paragraph that `revalidate` also
   requires the concern evidence's `PATH:LINE` citations and quoted
   identifiers to hold in those files, that `sync` re-runs that check, and
   that concerns revalidated before this check must be revalidated again.
5. Run the focused commands, then all guardrails. Commit
   `feat(repair-queue): require a passing evidence check for promotion`.
