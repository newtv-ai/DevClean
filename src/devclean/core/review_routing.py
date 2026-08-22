"""Route unresolved-but-reviewable observations into the AI decision lane.

A lack of a local rule is not the same as a hard protection boundary. DevClean
keeps program payload, application state, Windows-managed storage, explicit KEEP
rules and other protected evidence out of AI deletion authority. Residual files
that are merely ambiguous may be reviewed by AI and, after a validated answer,
become reusable learned rules.
"""

from __future__ import annotations

from dataclasses import replace

from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageItem,
)

_REVIEWABLE_AMBIGUITY_TAGS = frozenset(
    {
        "byproduct",
        "cache_directory",
        "path_heuristic",
        "unknown",
        "legacy_age_based_review",
        "manual_review",
        "unmatched_file",
    }
)
_HARD_PROTECTION_TAGS = frozenset(
    {
        "installed_payload",
        "program_payload",
        "application_state",
        "system_managed",
        "application_keep",
    }
)


def route_unresolved_file_to_ai(item: TriageItem) -> TriageItem:
    """Promote reviewable ambiguity to AI review without authorizing deletion."""

    if item.target_kind is not CleanupTargetKind.FILE:
        return item
    if item.lane is not ReviewLane.REPORT_ONLY:
        return item
    tags = frozenset(item.tags)
    if tags & _HARD_PROTECTION_TAGS:
        return item
    if not tags & _REVIEWABLE_AMBIGUITY_TAGS:
        return item
    return replace(
        item,
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.MEDIUM,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        reason=item.reason + "；本地规则无法确定，进入 AI 复核候选，不会自动删除",
        tags=(*item.tags, "ai_review_candidate"),
    )


__all__ = ["route_unresolved_file_to_ai"]
