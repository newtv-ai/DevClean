"""Fresh semantic gate for application-owned whole-directory cleanup.

Catalog discovery grants *where* a whole tree may be considered. This module
re-checks that the exact audited TOOL rule still owns that boundary and that a
fresh, complete aggregate can still be collected before mutation.

Age, size and rebuild-cost heuristics are deliberately not safety gates here.
They may affect ranking or recommendation text, but once a vendor/source-backed
rule proves a tree is regenerable, a small or recently-used cache does not become
persistent user data. Runtime process guards are enforced separately by the
post-scan executor immediately before mutation.
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
    evaluate_application_path,
)
from devclean.core.cleanup_catalog import CleanupPolicy, KnownCleanupRoot, known_root_for_path
from devclean.scanner.filesystem import ScanOptions, ScanRecordKind, scan_roots


class WholeTreePolicyRefusal(ValueError):
    """An audited application root is not semantically eligible for mutation."""


@dataclass(frozen=True, slots=True)
class WholeTreePolicyEvidence:
    files: int
    logical_bytes: int
    latest_activity_time_ns: int


def _require_application_whole_tree_rule(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> ApplicationCleanupRule | None:
    """Return the exact audited rule before any expensive subtree work begins."""

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
    return rule


def assess_application_whole_tree_policy(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    evidence: WholeTreePolicyEvidence,
) -> WholeTreePolicyEvidence | None:
    """Validate semantic authority against already-collected tree evidence.

    Scan-time code may collect only aggregate metadata for a known whole-tree
    cache instead of classifying every child file. Execution-time code collects
    a fresh aggregate again. Both paths call this function, so performance work
    cannot silently widen the mutation boundary.

    The application evaluator is still consulted when it can reproduce its own
    last-use semantics, but its age/benefit recommendation is not allowed to
    revoke an otherwise source-audited TOOL ownership decision. Process state is
    intentionally supplied as ``False`` here; the executor performs the live
    process check immediately before deletion.
    """

    rule = _require_application_whole_tree_rule(path, known_roots)
    if rule is None:
        return None

    observed = datetime.fromtimestamp(
        evidence.latest_activity_time_ns / 1_000_000_000,
        tz=UTC,
    )
    decision = evaluate_application_path(
        path,
        logical_size=evidence.logical_bytes,
        last_used=observed,
        process_running=False,
    )
    if decision is not None and decision.rule.rule_id == rule.rule_id:
        if decision.rule.owner is not DecisionOwner.TOOL:
            raise WholeTreePolicyRefusal(
                "application evaluator no longer classifies the exact root as TOOL-owned"
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

    rule = _require_application_whole_tree_rule(path, known_roots)
    if rule is None:
        return None
    evidence = _fresh_tree_evidence(path)
    return assess_application_whole_tree_policy(path, known_roots, evidence)


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
