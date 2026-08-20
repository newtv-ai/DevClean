from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from devclean.core.cleanup_catalog import KnownCleanupRoot
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy
from devclean.core.triage import (
    Actionability,
    ExecutionPolicy,
    ReviewLane,
    TriageItem,
    TriageSession,
    triage_directory,
    triage_file,
)
from devclean.core.user_rules import DecisionRule, RuleMatch, UserRules, default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind
from devclean.ui import app

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _file_record(path: Path, *, age_days: int = 0, size: int = 4096) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=2,
        logical_size=size,
        allocated_size=size,
        raw_allocated_size=size,
        volume_serial=1,
        file_id="1" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0,
        creation_time_ns=1,
        last_write_time_ns=int((_NOW - timedelta(days=age_days)).timestamp() * 1_000_000_000),
    )


def _directory_record(path: Path, *, root: Path) -> ScanRecord:
    return ScanRecord(
        root=str(root),
        path=str(path),
        kind=ScanRecordKind.DIRECTORY,
        depth=1,
        logical_size=0,
        allocated_size=0,
        raw_allocated_size=0,
        volume_serial=1,
        file_id="2" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10,
        creation_time_ns=1,
        last_write_time_ns=int(_NOW.timestamp() * 1_000_000_000),
    )


def _triage(
    path: Path,
    *,
    age_days: int = 0,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> TriageItem:
    rules = default_rules()
    return triage_file(
        _file_record(path, age_days=age_days),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=path.parents[1] / "different-temp-root",
        known_roots=known_roots,
    )


def _assert_protected(item: TriageItem) -> None:
    assert item.lane is ReviewLane.REPORT_ONLY
    assert item.actionability is Actionability.REPORT_ONLY
    assert item.execution_policy is ExecutionPolicy.NONE
    assert not app.is_direct_cleanup_eligible(item)
    assert not app.is_user_review_eligible(item)
    assert not app.is_ai_review_eligible(item)


def test_generic_filename_cache_and_development_hints_are_protected(tmp_path: Path) -> None:
    cases = (
        tmp_path / "opaque" / "diagnostics.log",
        tmp_path / "opaque" / "library.pdb",
        tmp_path / "cache" / "payload.bin",
        tmp_path / "huggingface" / "opaque.bin",
        tmp_path / "target" / "artifact.blob",
        tmp_path / "Downloads" / "installer.iso",
        tmp_path / "unknown" / "mystery.blob",
    )
    for path in cases:
        path.parent.mkdir(parents=True, exist_ok=True)
        _assert_protected(_triage(path))


def test_recent_age_based_root_is_kept_without_asking_user(tmp_path: Path) -> None:
    root = tmp_path / "known-temp"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.USER_TEMP,
        policy=CleanupPolicy.AGE_BASED_REVIEW,
        label="Known temp",
    )
    recent = root / "recent.tmp"
    old = root / "old.tmp"

    _assert_protected(_triage(recent, age_days=0, known_roots=(known,)))
    old_item = _triage(old, age_days=3, known_roots=(known,))
    assert old_item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert app.is_direct_cleanup_eligible(old_item)


def test_legacy_manual_review_root_is_protected_even_from_old_sidecar(tmp_path: Path) -> None:
    root = tmp_path / "legacy-manual"
    root.mkdir()
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.MANUAL_REVIEW,
        label="Legacy manual root",
    )
    _assert_protected(_triage(root / "payload.bin", known_roots=(known,)))


def test_generic_stale_version_and_tool_output_directories_are_not_user_delete_lanes(
    tmp_path: Path,
) -> None:
    rules = default_rules()
    versions = tmp_path / "versions"
    old = versions / "1.0.0"
    current = versions / "2.0.0"
    old.mkdir(parents=True)
    current.mkdir()
    stale = triage_directory(
        _directory_record(old, root=tmp_path),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    assert stale is not None
    _assert_protected(stale)

    project = tmp_path / "project"
    node_modules = project / "node_modules"
    node_modules.mkdir(parents=True)
    generated = triage_directory(
        _directory_record(node_modules, root=tmp_path),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
    )
    if generated is not None:
        _assert_protected(generated)


def test_learned_delete_rule_cannot_promote_generic_protected_path(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "opaque.log"
    path.parent.mkdir()
    item = _triage(path)
    _assert_protected(item)

    base = default_rules()
    rules = UserRules(
        scan=base.scan,
        delete=replace(
            base.delete,
            rules=(
                DecisionRule(
                    rule_id="legacy-delete",
                    group="ai_import",
                    match=RuleMatch.EXACT_PATH,
                    value=item.path,
                    source="AI_IMPORT",
                    reason="old heuristic verdict",
                ),
            ),
        ),
        keep=base.keep,
    )
    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)
    session.observe_path(item.path, rules)
    session.add(item)

    deletable, unsure = app._partition_items(session, rules)
    assert deletable == ()
    assert unsure == ()


def test_packaged_scan_roots_no_longer_delegate_broad_raw_paths_to_manual_review() -> None:
    rules = default_rules()
    assert not any(
        CleanupPolicy(root.policy) is CleanupPolicy.MANUAL_REVIEW
        for root in rules.scan.known_cleanup_roots
    )
    assert rules.delete.classification.inferred_ai_review_categories == frozenset()
