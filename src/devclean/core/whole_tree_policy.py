"""Fresh semantic gate for application-owned whole-directory cleanup.

Catalog discovery grants *where* a whole tree may be considered. This module
re-checks *whether it is worth removing now*. The same policy evaluator is used
by both the fast scan summary and the final execution-time revalidation so the
UI does not invent a second decision rule.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    PolicyAction,
    effective_idle_days,
    evaluate_application_path,
)
from devclean.core.cleanup_catalog import CleanupPolicy, KnownCleanupRoot, known_root_for_path
from devclean.scanner.filesystem import ScanOptions, ScanRecordKind, scan_roots


class WholeTreePolicyRefusal(ValueError):
    """An audited application root is not semantically eligible right now."""


@dataclass(frozen=True, slots=True)
class WholeTreePolicyEvidence:
    files: int
    logical_bytes: int
    latest_activity_time_ns: int


def assess_application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    evidence: WholeTreePolicyEvidence,
) -> WholeTreePolicyEvidence | None:
    """Apply the audited whole-tree rule to already-collected metadata evidence.

    Scan-time code may collect only aggregate metadata for a known whole-tree
    cache instead of classifying every child file. Execution-time code collects
    a fresh aggregate again. Both paths call this function, so performance work
    cannot silently change the product rule.
    """

    known = known_root_for_path(path, known_roots)
    if known is None or _normalized(known.path) != _normalized(path):
        return None
    rule = known.application_rule
    if rule is None:
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            raise WholeTreePolicyRefusal(
                "static vendor-managed roots do not carry whole-tree mutation authority"
            )
        return None
    if rule.owner is not DecisionOwner.TOOL or not rule.allow_whole_tree:
        raise WholeTreePolicyRefusal(
            "application whole-tree authority is no longer a deletable TOOL rule"
        )

    observed = datetime.fromtimestamp(
        evidence.latest_activity_time_ns / 1_000_000_000,
        tz=UTC,
    )
    _require_fresh_tree_floor(rule, evidence, observed)

    decision = evaluate_application_path(
        path,
        logical_size=evidence.logical_bytes,
        last_used=observed,
        process_running=False,
    )
    if decision is not None and decision.rule.rule_id == rule.rule_id:
        if decision.action is not PolicyAction.TOOL_DELETE:
            raise WholeTreePolicyRefusal(
                f"{rule.label} is not currently eligible: {decision.action.value}"
            )
        return evidence

    if rule.last_use not in {
        LastUseStrategy.FILE_MTIME,
        LastUseStrategy.DIRECTORY_MTIME,
    }:
        raise WholeTreePolicyRefusal(
            "application-specific last-use evidence cannot be re-established"
        )
    return evidence


def require_application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> WholeTreePolicyEvidence | None:
    """Collect fresh evidence and require the retained application TOOL policy."""

    known = known_root_for_path(path, known_roots)
    if known is None or _normalized(known.path) != _normalized(path):
        return None
    evidence = _fresh_tree_evidence(path)
    return assess_application_whole_tree_policy(path, known_roots, evidence)


def _require_fresh_tree_floor(
    rule: ApplicationCleanupRule,
    evidence: WholeTreePolicyEvidence,
    observed: datetime,
) -> None:
    if evidence.logical_bytes < rule.min_reclaim_bytes:
        raise WholeTreePolicyRefusal(f"{rule.label} is below its minimum reclaim threshold")
    threshold = effective_idle_days(rule, evidence.logical_bytes)
    if threshold is None:
        raise WholeTreePolicyRefusal("application whole-tree idle threshold is unavailable")
    idle_days = max(
        0.0,
        (datetime.now(UTC) - observed).total_seconds() / 86_400,
    )
    if idle_days < threshold:
        raise WholeTreePolicyRefusal(f"{rule.label} was used too recently for whole-tree cleanup")


def _fresh_tree_evidence(path: Path) -> WholeTreePolicyEvidence:
    files = 0
    logical_bytes = 0
    latest_ns: int | None = None
    for record in scan_roots(
        (path,),
        ScanOptions(
            include_directories=True,
            exact_file_identity=False,
            deduplicate_hardlinks=False,
        ),
    ):
        if record.kind is ScanRecordKind.ERROR:
            raise WholeTreePolicyRefusal(
                f"fresh whole-tree policy scan was incomplete at {record.path}"
            )
        for timestamp in (record.creation_time_ns, record.last_write_time_ns):
            if timestamp is not None:
                latest_ns = timestamp if latest_ns is None else max(latest_ns, timestamp)
        if record.kind is ScanRecordKind.FILE:
            files += 1
            logical_bytes += record.logical_size
    if latest_ns is None:
        raise WholeTreePolicyRefusal(
            "fresh whole-tree policy scan has no reliable last-use timestamp"
        )
    return WholeTreePolicyEvidence(files, logical_bytes, latest_ns)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "WholeTreePolicyEvidence",
    "WholeTreePolicyRefusal",
    "assess_application_whole_tree_policy",
    "require_application_whole_tree_policy",
]
