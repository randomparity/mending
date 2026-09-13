"""Internal batch assembly boundary for holistic review preparation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .prepare_batches_collectors import _DIMENSION_FILE_MAPPING, _FILE_COLLECTORS
from .prepare_batches_core import _ensure_holistic_context
from .prepare_holistic_scope import (
    file_in_allowed_scope,
    filter_batches_to_file_scope,
)

_CONCERN_BATCH_DIMENSION = "design_coherence"
_DEFAULT_REVIEW_BATCH_MAX_FILES = 80


def _unique_paths(paths: list[object], limit: int) -> list[str]:
    result: list[str] = []
    for path in paths:
        if not isinstance(path, str) or not path or path in result:
            continue
        result.append(path)
        if len(result) == limit:
            break
    return result


def _neighbor_paths(paths: list[str], lang: object) -> list[str]:
    graph = getattr(lang, "dep_graph", None)
    if not isinstance(graph, dict):
        return []
    neighbors: list[str] = []
    for path in paths:
        entry = graph.get(path)
        if not isinstance(entry, dict):
            continue
        for key in ("imports", "importers"):
            values = entry.get(key, ())
            if isinstance(values, (set, list, tuple)):
                neighbors.extend(sorted(item for item in values if isinstance(item, str)))
    return neighbors


def _apply_bounded_reading_sets(
    batches: list[dict[str, Any]],
    *,
    holistic_ctx: Any,
    lang: object,
    state: dict,
    all_files: list[str],
    allowed_review_files: set[str],
    max_files_per_batch: int | None,
) -> None:
    """Attach a deterministic, bounded reading set to each investigation batch."""
    cap = max_files_per_batch or _DEFAULT_REVIEW_BATCH_MAX_FILES
    context = _ensure_holistic_context(holistic_ctx)
    scan_count = state.get("scan_count", 0)
    rotation = scan_count if isinstance(scan_count, int) else 0
    for batch in batches:
        seeds = list(batch.get("files_to_read", []))
        for dimension in batch.get("dimensions", []):
            collector = _FILE_COLLECTORS.get(_DIMENSION_FILE_MAPPING.get(dimension, ""))
            if callable(collector):
                seeds.extend(collector(context, max_files=cap))
        selected = _unique_paths(
            [path for path in seeds if file_in_allowed_scope(path, allowed_review_files)],
            cap,
        )
        selected.extend(
            path
            for path in _neighbor_paths(selected, lang)
            if file_in_allowed_scope(path, allowed_review_files)
        )
        selected = _unique_paths(selected, cap)
        remaining = [
            path
            for path in sorted(all_files)
            if path not in selected and file_in_allowed_scope(path, allowed_review_files)
        ]
        if remaining:
            start = rotation % len(remaining)
            selected = _unique_paths(selected + remaining[start:] + remaining[:start], cap)
        batch["files_to_read"] = selected


@dataclass(frozen=True)
class HolisticBatchAssemblyDependencies:
    """Injected collaborators for holistic batch assembly."""

    build_investigation_batches_fn: object
    batch_concerns_fn: object
    filter_batches_to_dimensions_fn: object
    append_full_sweep_batch_fn: object
    log_best_effort_failure_fn: object
    logger: object


def _merge_batch_payload(
    batches: list[dict[str, Any]],
    incoming_batch: dict[str, Any],
) -> None:
    """Merge concern payload into an existing dimension batch when available."""
    incoming_dimensions = incoming_batch.get("dimensions")
    for existing in batches:
        if existing.get("dimensions") != incoming_dimensions:
            continue
        existing["concern_signals"] = incoming_batch.get("concern_signals", [])
        existing["concern_signal_count"] = incoming_batch.get("concern_signal_count", 0)
        existing["files_to_read"] = incoming_batch.get("files_to_read", [])
        judgment_counts = incoming_batch.get("judgment_finding_counts")
        if judgment_counts:
            existing["judgment_finding_counts"] = judgment_counts
        return
    batches.append(incoming_batch)


def _append_concerns_batch(
    batches: list[dict[str, Any]],
    state: dict,
    dims: list[str],
    allowed_review_files: set[str],
    max_files_per_batch: int | None,
    *,
    batch_concerns_fn,
    log_best_effort_failure_fn,
    log: object,
) -> None:
    """Append concern-signal evidence when the active dimensions can consume it."""
    if _CONCERN_BATCH_DIMENSION not in dims:
        return
    try:
        from desloppify.engine._concerns.generators import generate_concerns

        concerns = generate_concerns(state)
        concerns = [
            concern
            for concern in concerns
            if file_in_allowed_scope(getattr(concern, "file", ""), allowed_review_files)
        ]
        concerns_batch = batch_concerns_fn(
            concerns,
            max_files=max_files_per_batch,
            active_dimensions=dims,
        )
        if concerns_batch:
            _merge_batch_payload(batches, concerns_batch)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        log_best_effort_failure_fn(log, "generate review concern batch", exc)


def assemble_holistic_batches(
    holistic_ctx,
    *,
    lang: object,
    repo_root: Path,
    state: dict,
    dims: list[str],
    all_files: list[str],
    allowed_review_files: set[str],
    include_full_sweep: bool,
    max_files_per_batch: int | None,
    deps: HolisticBatchAssemblyDependencies,
) -> list[dict[str, Any]]:
    """Build, enrich, and scope holistic investigation batches in one place."""
    batches = deps.build_investigation_batches_fn(
        holistic_ctx,
        lang,
        repo_root=repo_root,
        max_files_per_batch=max_files_per_batch,
        state=state,
    )

    _append_concerns_batch(
        batches,
        state,
        dims,
        allowed_review_files,
        max_files_per_batch,
        batch_concerns_fn=deps.batch_concerns_fn,
        log_best_effort_failure_fn=deps.log_best_effort_failure_fn,
        log=deps.logger,
    )
    _apply_bounded_reading_sets(
        batches,
        holistic_ctx=holistic_ctx,
        lang=lang,
        state=state,
        all_files=all_files,
        allowed_review_files=allowed_review_files,
        max_files_per_batch=max_files_per_batch,
    )

    batches = deps.filter_batches_to_dimensions_fn(
        batches,
        dims,
        fallback_max_files=max_files_per_batch,
    )
    if include_full_sweep:
        deps.append_full_sweep_batch_fn(
            batches=batches,
            dims=dims,
            all_files=all_files,
            lang=lang,
            max_files=max_files_per_batch,
        )
    return filter_batches_to_file_scope(
        batches,
        allowed_files=allowed_review_files,
    )


__all__ = [
    "HolisticBatchAssemblyDependencies",
    "assemble_holistic_batches",
]
