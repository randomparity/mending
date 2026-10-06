"""Rank selectable small repairs on stored source evidence (ADR 0014); never a score."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from desloppify.engine.repair_queue import PromotionCandidate, matching_record


def rank_key(
    issue: Mapping[str, Any], candidate: PromotionCandidate
) -> tuple[int, int, int, int, str]:
    """Risk, verification feasibility, evidence, benefit, then key; the lowest ranks first."""
    record = matching_record(issue["detail"], "github_repair_revalidated", candidate) or {}
    citations = [
        citation
        for claim in record.get("check", {}).get("claims", [])
        for citation in claim["citations"]
    ]
    return (
        len(record.get("manifest", {}).get("dependencies", [])),
        0 if candidate.route == "finding" else 1,
        -len(citations),
        -sum(citation["end"] - citation["start"] + 1 for citation in citations),
        candidate.key,
    )


__all__ = ["rank_key"]
