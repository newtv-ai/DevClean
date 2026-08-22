"""Fresh semantic gate for application-owned whole-directory cleanup.

Catalog discovery grants *where* a whole tree may be considered. This module
re-checks *whether it is worth removing now*. Cheap and moderate rebuild caches
are allowed to clean immediately once their owning process is closed; expensive
indexes/models retain an idle-time floor so the one-click cleaner does not trade
space for a large rebuild cost.
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
    RebuildCost,
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


def require_application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> WholeTreePolicyEvidence | None:
    """Require the retained application TOOL policy for an exact known root.

    ``None`` means this is not an application-derived whole-tree root, so the
    existing configured/system directory policy remains unchanged. Application
    roots are rescanned so execution still uses current size/activity evidence.
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

    evidence = _fresh_tree_evidence(path)
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
        # For cheap/moderate regenerable caches, recent use is a performance
        # preference rather than a data-safety boundary. The one-click cleaner
        # therefore keeps the audited TOOL authority even if the app evaluator
        # recommends KEEP_RECENT. Process-closed requirements are still enforced
        # immediately before mutation in postscan_cleanup.
        if (
            decision.action is not PolicyAction.TOOL_DELETE
            and rule.rebuild_cost is RebuildCost.HIGH
        ):
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


def _require_fresh_tree_floor(
    rule: ApplicationCleanupRule,
    evidence: WholeTreePolicyEvidence,
    observed: datetime,
) -> None:
    if evidence.logical_bytes < rule.min_reclaim_bytes:
        raise WholeTreePolicyRefusal(f"{rule.label} is below its minimum reclaim threshold")

    # LOW/MEDIUM roots are explicitly audited, regenerable caches. Refusing them
    # merely because they were touched recently made a scan advertise dozens of
    # "safe" rows and then reject nearly all of them during cleanup. Expensive
    # HIGH rebuild roots (IDE indexes, model-like artifacts, etc.) retain the
    # original idle threshold and are also omitted from the default smart scan.
    if rule.rebuild_cost is not RebuildCost.HIGH:
        return

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
    "require_application_whole_tree_policy",
]
