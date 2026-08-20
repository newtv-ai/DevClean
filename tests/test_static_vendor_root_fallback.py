from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from devclean.core.application_cleanup import CLAUDE_RULES
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.postscan_cleanup import CleanupRefusal, _require_directory_scope
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageItem,
    triage_directory,
    triage_file,
)
from devclean.core.user_rules import default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _record(path: Path, *, directory: bool, root: Path | None = None) -> ScanRecord:
    return ScanRecord(
        root=str(root or path.parent),
        path=str(path),
        kind=ScanRecordKind.DIRECTORY if directory else ScanRecordKind.FILE,
        depth=1,
        logical_size=0 if directory else 4096,
        allocated_size=0 if directory else 4096,
        raw_allocated_size=0 if directory else 4096,
        volume_serial=1,
        file_id=("2" if directory else "1") * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        creation_time_ns=1,
        last_write_time_ns=int(_NOW.timestamp() * 1_000_000_000),
    )


def _assert_protected(item: TriageItem | None) -> None:
    assert item is not None
    assert item.lane is ReviewLane.REPORT_ONLY
    assert item.risk_tier is RiskTier.PROTECTED
    assert item.actionability is Actionability.REPORT_ONLY
    assert item.execution_policy is ExecutionPolicy.NONE


def test_packaged_scan_roots_are_discovery_only_not_static_vendor_authority() -> None:
    rules = default_rules()
    assert not any(
        CleanupPolicy(root.policy) is CleanupPolicy.VENDOR_MANAGED
        for root in rules.scan.known_cleanup_roots
    )


def test_legacy_static_vendor_file_fails_closed(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "legacy-vendor-root"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Legacy vendor root",
    )
    item = triage_file(
        _record(root / "opaque.blob", directory=False),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=tmp_path / "different-temp",
    )
    _assert_protected(item)
    assert "missing_application_rule" in item.tags


def test_legacy_static_vendor_directory_fails_closed_and_execution_refuses(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "legacy-vendor-root"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Legacy vendor root",
        delete_root_itself=True,
    )
    item = triage_directory(
        _record(root, directory=True, root=root.parent),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    _assert_protected(item)
    assert item is not None
    assert item.target_kind is CleanupTargetKind.DIRECTORY

    with pytest.raises(CleanupRefusal, match="attached audited application TOOL rule"):
        _require_directory_scope(
            root,
            (known,),
            rules.delete.classification,
            rules.keep.classification,
        )


def test_attached_whole_tree_rule_does_not_authorize_unmatched_individual_file(
    tmp_path: Path,
) -> None:
    rules = default_rules()
    root = tmp_path / "audited-vendor-root"
    root.mkdir()
    application_rule = next(
        rule for rule in CLAUDE_RULES if rule.allow_whole_tree and rule.owner.value == "TOOL"
    )
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Audited vendor root",
        application_rule=application_rule,
    )
    item = triage_file(
        _record(root / "unmatched.blob", directory=False),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=tmp_path / "different-temp",
    )
    _assert_protected(item)
    assert "unmatched_file" in item.tags


def test_attached_audited_tool_rule_retains_vendor_directory_authority(tmp_path: Path) -> None:
    rules = default_rules()
    root = tmp_path / "audited-vendor-root"
    root.mkdir()
    application_rule = next(
        rule for rule in CLAUDE_RULES if rule.allow_whole_tree and rule.owner.value == "TOOL"
    )
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Audited vendor root",
        delete_root_itself=True,
        application_rule=application_rule,
    )
    item = triage_directory(
        _record(root, directory=True, root=root.parent),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert item is not None
    assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert item.actionability is Actionability.REVIEW_PLAN
    assert item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
    assert (
        _require_directory_scope(
            root,
            (known,),
            rules.delete.classification,
            rules.keep.classification,
        ).value
        == "KNOWN_CACHE_ROOT"
    )
