from __future__ import annotations

from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.review_routing import route_unresolved_file_to_ai
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.scanner import ScanRecord, ScanRecordKind


def _item(
    *,
    tags: tuple[str, ...],
    target_kind: CleanupTargetKind = CleanupTargetKind.FILE,
) -> TriageItem:
    record = ScanRecord(
        root="C:\\",
        path="C:\\Users\\tester\\mystery.cache",
        kind=ScanRecordKind.FILE,
        depth=2,
        logical_size=1024,
    )
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=record.logical_size,
        allocated_size=record.allocated_size,
        category=CleanupCategory.OTHER,
        source_domain=SourceDomain.GENERAL_STORAGE,
        lane=ReviewLane.REPORT_ONLY,
        risk_tier=RiskTier.PROTECTED,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.REPORT_ONLY,
        execution_policy=ExecutionPolicy.NONE,
        recovery=RecoveryCapability.UNKNOWN,
        reason="local rules cannot decide",
        tags=tags,
        target_kind=target_kind,
    )


def test_unknown_file_becomes_ai_review_candidate() -> None:
    routed = route_unresolved_file_to_ai(_item(tags=("unknown", "report_only")))

    assert routed.lane is ReviewLane.AI_REVIEW
    assert routed.actionability is Actionability.AI_REVIEW
    assert routed.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
    assert routed.risk_tier is RiskTier.MEDIUM
    assert "ai_review_candidate" in routed.tags


def test_program_payload_stays_hard_protected() -> None:
    original = _item(tags=("unknown", "program_payload", "report_only"))

    assert route_unresolved_file_to_ai(original) is original


def test_directory_is_not_promoted_to_ai_file_review() -> None:
    original = _item(tags=("unknown",), target_kind=CleanupTargetKind.DIRECTORY)

    assert route_unresolved_file_to_ai(original) is original
