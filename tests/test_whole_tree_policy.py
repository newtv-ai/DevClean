from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
)
from devclean.core.whole_tree_policy import (
    WholeTreePolicyRefusal,
    require_application_whole_tree_policy,
)
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

_MIB = 1024**2


def _rule(
    *,
    min_reclaim: int = 16 * _MIB,
    idle_days: float = 30,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id="test-browser-cache",
        app_id="test-browser",
        root_key="TEST_CACHE",
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.LOW,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim,
        requires_process_closed=True,
        allow_whole_tree=True,
        label="Test browser cache",
    )


def _known(path: Path, rule: ApplicationCleanupRule | None) -> KnownCleanupRoot:
    return KnownCleanupRoot(
        path=path,
        category=CleanupCategory.BROWSER_CACHE,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Test cache",
        delete_root_itself=True,
        application_rule=rule,
    )


def _records(path: Path, *, size: int, age_days: int) -> tuple[ScanRecord, ...]:
    now = datetime.now(UTC)
    old_root = int((now - timedelta(days=90)).timestamp() * 1_000_000_000)
    child_time = int((now - timedelta(days=age_days)).timestamp() * 1_000_000_000)
    return (
        ScanRecord(
            root=str(path),
            path=str(path),
            kind=ScanRecordKind.DIRECTORY,
            depth=0,
            last_write_time_ns=old_root,
        ),
        ScanRecord(
            root=str(path),
            path=str(path / "entry.bin"),
            kind=ScanRecordKind.FILE,
            depth=1,
            logical_size=size,
            last_write_time_ns=child_time,
        ),
    )


def _fake_scan(records: tuple[ScanRecord, ...]) -> object:
    return lambda *_args, **_kwargs: iter(records)


def test_recent_child_blocks_whole_tree_even_when_root_directory_is_old(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Cache"
    rule = _rule()
    monkeypatch.setattr(
        policy,
        "scan_roots",
        _fake_scan(_records(root, size=64 * _MIB, age_days=1)),
    )
    monkeypatch.setattr(
        policy,
        "evaluate_application_path",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(WholeTreePolicyRefusal, match="too recently"):
        require_application_whole_tree_policy(root, (_known(root, rule),))


def test_stale_large_application_tree_passes_and_returns_fresh_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Cache"
    rule = _rule()
    monkeypatch.setattr(
        policy,
        "scan_roots",
        _fake_scan(_records(root, size=64 * _MIB, age_days=45)),
    )
    monkeypatch.setattr(
        policy,
        "evaluate_application_path",
        lambda *_args, **_kwargs: None,
    )

    evidence = require_application_whole_tree_policy(root, (_known(root, rule),))

    assert evidence is not None
    assert evidence.files == 1
    assert evidence.logical_bytes == 64 * _MIB


def test_application_tree_below_reclaim_threshold_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Cache"
    rule = _rule(min_reclaim=16 * _MIB)
    monkeypatch.setattr(
        policy,
        "scan_roots",
        _fake_scan(_records(root, size=2 * _MIB, age_days=90)),
    )
    monkeypatch.setattr(
        policy,
        "evaluate_application_path",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(WholeTreePolicyRefusal, match="minimum reclaim"):
        require_application_whole_tree_policy(root, (_known(root, rule),))


def test_incomplete_fresh_scan_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Cache"
    records = (
        *_records(root, size=64 * _MIB, age_days=90),
        ScanRecord(
            root=str(root),
            path=str(root / "locked"),
            kind=ScanRecordKind.ERROR,
            depth=1,
            error="access denied",
        ),
    )
    monkeypatch.setattr(policy, "scan_roots", _fake_scan(records))

    with pytest.raises(WholeTreePolicyRefusal, match="incomplete"):
        require_application_whole_tree_policy(root, (_known(root, _rule()),))


def test_configured_vendor_root_without_application_rule_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Windows.old"

    def must_not_scan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("static vendor roots must fail before application policy scan")

    monkeypatch.setattr(policy, "scan_roots", must_not_scan)

    with pytest.raises(WholeTreePolicyRefusal, match="static vendor-managed"):
        require_application_whole_tree_policy(root, (_known(root, None),))


def test_non_mtime_rule_without_native_evaluator_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.core.whole_tree_policy as policy

    root = tmp_path / "Cache"
    rule = _rule()
    rule = ApplicationCleanupRule(
        rule_id=rule.rule_id,
        app_id=rule.app_id,
        root_key=rule.root_key,
        relative_pattern=rule.relative_pattern,
        match_kind=rule.match_kind,
        owner=rule.owner,
        last_use=LastUseStrategy.APP_ACTIVITY,
        rebuild_cost=rule.rebuild_cost,
        idle_days=rule.idle_days,
        min_reclaim_bytes=rule.min_reclaim_bytes,
        requires_process_closed=rule.requires_process_closed,
        allow_whole_tree=rule.allow_whole_tree,
        label=rule.label,
    )
    monkeypatch.setattr(
        policy,
        "scan_roots",
        _fake_scan(_records(root, size=64 * _MIB, age_days=90)),
    )
    monkeypatch.setattr(
        policy,
        "evaluate_application_path",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(WholeTreePolicyRefusal, match="cannot be re-established"):
        require_application_whole_tree_policy(root, (_known(root, rule),))
