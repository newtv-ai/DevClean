"""Fresh semantic gate for application-owned whole-directory cleanup.

Catalog discovery grants *where* a whole tree may be considered.  This module
re-checks *whether it is worth removing now*.  It deliberately performs a fresh,
read-only subtree inventory at the capability boundary so a cache used after the
main scan cannot inherit an old idle/benefit decision.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from devclean.core.application_cleanup import (
    DecisionOwner,
    LastUseStrategy,
    PolicyAction,
    effective_idle_days,
    evaluate_application_path,
)
from devclean.core.cleanup_catalog import KnownCleanupRoot, known_root_for_path
from devclean.scanner.filesystem import ScanOptions, ScanRecordKind, scan_roots


class WholeTreePolicyRefusal(ValueError):
    """An audited application root is not semantically eligible right now."""


@dataclass(frozen=True, slots=True)
class WholeTreePolicyEvidence:
    files: int
    logical_bytes: int
    latest_last_write_time_ns: int


def require_application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> WholeTreePolicyEvidence | None:
    """Require the retained application TOOL policy for an exact known root.

    ``None`` means this is not an application-derived whole-tree root, so the
    existing configured/system directory policy remains unchanged.  Application
    roots are rescanned because scan-time directory mtimes alone do not reveal a
    recently rewritten child file.
    """

    known = known_root_for_path(path, known_roots)
    if known is None or _normalized(known.path) != _normalized(path):
        return None
    rule = known.application_rule
    if rule is None:
        return None
    if rule.owner is not DecisionOwner.TOOL or not rule.allow_whole_tree:
        raise WholeTreePolicyRefusal(
            "application whole-tree authority is no longer a deletable TOOL rule"
        )

    evidence = _fresh_tree_evidence(path)
    observed = datetime.fromtimestamp(
        evidence.latest_last_write_time_ns / 1_000_000_000,
        tz=UTC,
    )

    # Prefer the application's own evaluator.  Fixed/default roots and special
    # last-use strategies (for example Codex APP_ACTIVITY) retain their native
    # semantics here.  A runtime-only redirected cache can disappear from live
    # discovery after its process exits, so a FILE/DIRECTORY_MTIME rule has a
    # conservative generic fallback using the retained audited rule itself.
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
    if evidence.logical_bytes < rule.min_reclaim_bytes:
        raise WholeTreePolicyRefusal(
            f"{rule.label} is below its minimum reclaim threshold"
        )
    threshold = effective_idle_days(rule, evidence.logical_bytes)
    if threshold is None:
        raise WholeTreePolicyRefusal(
            "application whole-tree idle threshold is unavailable"
        )
    idle_days = max(
        0.0,
        (datetime.now(UTC) - observed).total_seconds() / 86_400,
    )
    if idle_days < threshold:
        raise WholeTreePolicyRefusal(
            f"{rule.label} was used too recently for whole-tree cleanup"
        )
    return evidence


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
        if record.last_write_time_ns is not None:
            latest_ns = (
                record.last_write_time_ns
                if latest_ns is None
                else max(latest_ns, record.last_write_time_ns)
            )
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
