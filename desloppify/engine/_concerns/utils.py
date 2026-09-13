"""Small utility helpers for concern generation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .types import ConcernSignals, SignalKey


def _update_max_signal(signals: ConcernSignals, key: SignalKey, value: object) -> None:
    """Update numeric signal key with max(existing, value) when value is valid."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return
    current = float(signals.get(key, 0.0))
    signals[key] = max(current, float(value))


def _fingerprint(concern_type: str, file: str, key_signals: tuple[str, ...]) -> str:
    """Stable hash of (type, file, sorted key signals)."""
    raw = f"{concern_type}::{file}::{','.join(sorted(key_signals))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _comparison_digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _dismissal_comparison(
    state: dict[str, Any], concern_type: str, source_issue_ids: tuple[str, ...]
) -> tuple[str, str]:
    """Return path-independent identity and evidence for a generated concern."""
    issues = state.get("work_items") or state.get("issues", {})
    sources = [issues.get(issue_id, {}) for issue_id in source_issue_ids]
    detectors = sorted(str(source.get("detector", "")) for source in sources)
    fingerprints = sorted(
        _source_fingerprint(source) for source in sources if isinstance(source, dict)
    )
    return (
        _comparison_digest({"schema": 1, "type": concern_type, "detectors": detectors}),
        _comparison_digest({"schema": 1, "sources": fingerprints}),
    )


def _source_fingerprint(issue: dict[str, Any]) -> str:
    from desloppify.engine._state.filtering import issue_suppression_fingerprint

    return issue_suppression_fingerprint(issue)


def _is_dismissed(
    state: dict[str, Any],
    dismissals: dict[str, Any],
    concern_type: str,
    source_issue_ids: tuple[str, ...],
) -> bool:
    """Suppress only one dismissal with matching path-independent evidence."""
    identity, evidence = _dismissal_comparison(state, concern_type, source_issue_ids)
    matches = [
        entry
        for entry in dismissals.values()
        if isinstance(entry, dict)
        and entry.get("dismissal_identity") == identity
        and entry.get("dismissal_evidence_digest") == evidence
    ]
    return len(matches) == 1


__all__ = [
    "_dismissal_comparison",
    "_fingerprint",
    "_is_dismissed",
    "_update_max_signal",
]
