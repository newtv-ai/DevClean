from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
    known_root_for_path,
)
from devclean.core.triage import Actionability, ExecutionPolicy, ReviewLane, triage_file
from devclean.scanner import ScanRecord, ScanRecordKind


def _record(path: Path, *, last_write_time_ns: int | None = 0) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=100,
        allocated_size=4096,
        raw_allocated_size=4096,
        last_write_time_ns=last_write_time_ns,
    )


def test_catalog_discovers_only_existing_known_local_roots(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    roaming = tmp_path / "Roaming"
    user_temp = local / "Temp"
    pip_cache = local / "pip" / "Cache"
    crash_dumps = local / "CrashDumps"
    for directory in (user_temp, pip_cache, crash_dumps):
        directory.mkdir(parents=True)

    roots = discover_known_cleanup_roots(
        {"LOCALAPPDATA": str(local), "APPDATA": str(roaming)},
        home=tmp_path / "Home",
        temp_root=user_temp,
    )

    assert {(root.category, root.path) for root in roots} == {
        (CleanupCategory.USER_TEMP, user_temp),
        (CleanupCategory.PIP_CACHE, pip_cache),
        (CleanupCategory.CRASH_DUMPS, crash_dumps),
    }


def test_catalog_prefers_the_most_specific_known_root(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    nested = root / "pip"
    target = nested / "wheel.bin"
    nested.mkdir(parents=True)
    catalog = (
        KnownCleanupRoot(root, CleanupCategory.OTHER, CleanupPolicy.REPORT_ONLY, "generic"),
        KnownCleanupRoot(
            nested, CleanupCategory.PIP_CACHE, CleanupPolicy.VENDOR_MANAGED, "pip"
        ),
    )

    matched = known_root_for_path(target, catalog)

    assert matched is not None
    assert matched.category is CleanupCategory.PIP_CACHE


def test_catalog_discovers_browser_cache_but_not_browser_profile_data(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    profile = local / "Google" / "Chrome" / "User Data" / "Default"
    cache = profile / "Cache"
    cache.mkdir(parents=True)
    (profile / "History").write_text("never a cache-root candidate", encoding="utf-8")

    roots = discover_known_cleanup_roots(
        {"LOCALAPPDATA": str(local)}, home=tmp_path / "Home", temp_root=tmp_path / "Temp"
    )

    assert any(
        root.path == cache and root.category is CleanupCategory.BROWSER_CACHE for root in roots
    )
    assert next(root for root in roots if root.path == cache).policy is CleanupPolicy.MANUAL_REVIEW
    assert not any(root.path == profile for root in roots)


def test_catalog_honors_existing_cache_environment_overrides(tmp_path: Path) -> None:
    custom_pip = tmp_path / "custom-pip"
    custom_hf = tmp_path / "custom-hf" / "hub"
    custom_pip.mkdir()
    custom_hf.mkdir(parents=True)

    roots = discover_known_cleanup_roots(
        {"PIP_CACHE_DIR": str(custom_pip), "HF_HUB_CACHE": str(custom_hf)},
        home=tmp_path / "Home",
        temp_root=tmp_path / "Temp",
    )

    assert any(
        root.path == custom_pip and root.category is CleanupCategory.PIP_CACHE for root in roots
    )
    assert any(
        root.path == custom_hf.parent and root.category is CleanupCategory.HUGGINGFACE_CACHE
        for root in roots
    )


def test_known_cache_and_old_crash_dump_have_distinct_cleanup_lanes(tmp_path: Path) -> None:
    pip_root = tmp_path / "pip" / "Cache"
    dump_root = tmp_path / "CrashDumps"
    pip_root.mkdir(parents=True)
    dump_root.mkdir()
    catalog = (
        KnownCleanupRoot(
            pip_root, CleanupCategory.PIP_CACHE, CleanupPolicy.VENDOR_MANAGED, "pip 缓存"
        ),
        KnownCleanupRoot(
            dump_root,
            CleanupCategory.CRASH_DUMPS,
            CleanupPolicy.AGE_BASED_REVIEW,
            "用户崩溃转储",
        ),
    )
    now = datetime(2026, 7, 12, tzinfo=UTC)

    pip_item = triage_file(_record(pip_root / "wheel.whl"), now=now, known_roots=catalog)
    dump_item = triage_file(_record(dump_root / "app.dmp"), now=now, known_roots=catalog)

    assert pip_item.lane is ReviewLane.AI_REVIEW
    assert pip_item.actionability is Actionability.AI_REVIEW
    assert pip_item.execution_policy is ExecutionPolicy.RECYCLE_ONLY
    assert pip_item.category is CleanupCategory.PIP_CACHE
    assert dump_item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert dump_item.category is CleanupCategory.CRASH_DUMPS
