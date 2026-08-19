from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import devclean.core.jetbrains_leftover_maintenance as leftovers
from devclean.core.jetbrains_leftover_maintenance import (
    JetBrainsLeftoverInventory,
    JetBrainsTreeStats,
    cleanup_jetbrains_expired_system_directory,
    inventory_jetbrains_expired_system_directories,
)
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactRootBoundary,
)


def _snapshot(seed: int) -> ExactDirectorySnapshot:
    return ExactDirectorySnapshot(
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="128",
        creation_time_ns=1000 + seed,
    )


def _inventory(*, stats: JetBrainsTreeStats | None = None) -> JetBrainsLeftoverInventory:
    return JetBrainsLeftoverInventory(
        selector="IntelliJIdea2025.1",
        config_root=Path(r"C:\Users\test\AppData\Roaming\JetBrains\IntelliJIdea2025.1"),
        system_root=Path(r"C:\Users\test\AppData\Local\JetBrains\IntelliJIdea2025.1"),
        config_identity=_snapshot(1),
        system_identity=_snapshot(2),
        stats=stats
        or JetBrainsTreeStats(
            logical_bytes=4 * 1024**3,
            entry_count=100,
            latest_write_time_ns=1,
        ),
        stale_days=200.0,
        vendor_expired=True,
        installed=False,
        cleanup_supported=True,
        reason="expired",
    )


def _set_tree_mtime(root: Path, when: datetime) -> None:
    timestamp = when.timestamp()
    for current, directories, files in os.walk(root, topdown=False):
        base = Path(current)
        for name in files:
            os.utime(base / name, (timestamp, timestamp))
        for name in directories:
            os.utime(base / name, (timestamp, timestamp))
        os.utime(base, (timestamp, timestamp))


def _make_default_version(
    tmp_path: Path,
    selector: str,
    *,
    last_updated: datetime,
) -> tuple[dict[str, str], Path, Path]:
    appdata = tmp_path / "Roaming"
    localappdata = tmp_path / "Local"
    config_root = appdata / "JetBrains" / selector
    system_root = localappdata / "JetBrains" / selector
    config_root.mkdir(parents=True)
    (config_root / "options").mkdir()
    (config_root / "options" / "other.xml").write_text("config", encoding="utf-8")
    system_root.mkdir(parents=True)
    (system_root / "index").mkdir()
    (system_root / "index" / "data.bin").write_bytes(b"x" * 4096)
    _set_tree_mtime(system_root, last_updated)
    return (
        {"APPDATA": str(appdata), "LOCALAPPDATA": str(localappdata)},
        config_root,
        system_root,
    )


def test_inventory_marks_uninstalled_default_system_tree_expired_after_180_days(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    now = datetime(2026, 8, 19, tzinfo=UTC)
    environment, config_root, system_root = _make_default_version(
        tmp_path,
        "IntelliJIdea2025.1",
        last_updated=now - timedelta(days=200),
    )

    inventories = inventory_jetbrains_expired_system_directories(environment, now=now)

    assert len(inventories) == 1
    item = inventories[0]
    assert item.config_root == config_root.resolve()
    assert item.system_root == system_root.resolve()
    assert item.vendor_expired
    assert item.installed is False
    assert item.cleanup_supported
    assert item.stats.logical_bytes == 4096
    assert item.stats.entry_count >= 3
    assert item.stale_days >= 199


def test_inventory_keeps_recent_system_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    now = datetime(2026, 8, 19, tzinfo=UTC)
    environment, _, _ = _make_default_version(
        tmp_path,
        "PyCharm2026.1",
        last_updated=now - timedelta(days=30),
    )

    item = inventory_jetbrains_expired_system_directories(environment, now=now)[0]

    assert not item.vendor_expired
    assert not item.cleanup_supported
    assert "180" in item.reason


def test_inventory_protects_existing_installed_product_even_when_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    now = datetime(2026, 8, 19, tzinfo=UTC)
    environment, _, system_root = _make_default_version(
        tmp_path,
        "CLion2025.1",
        last_updated=now - timedelta(days=220),
    )
    home = tmp_path / "CLion-install"
    home.mkdir()
    (home / "product-info.json").write_text(
        json.dumps({"dataDirectoryName": "CLion2025.1"}),
        encoding="utf-8",
    )
    (system_root / ".home").write_text(str(home), encoding="utf-8")
    _set_tree_mtime(system_root, now - timedelta(days=220))

    item = inventory_jetbrains_expired_system_directories(environment, now=now)[0]

    assert item.vendor_expired
    assert item.installed is True
    assert not item.cleanup_supported
    assert "仍对应" in item.reason


def test_inventory_ignores_unrecognized_product_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    now = datetime(2026, 8, 19, tzinfo=UTC)
    environment, _, _ = _make_default_version(
        tmp_path,
        "SomeOtherTool2025.1",
        last_updated=now - timedelta(days=300),
    )

    assert inventory_jetbrains_expired_system_directories(environment, now=now) == ()


def test_installed_state_treats_missing_product_info_as_installed_self_build(
    tmp_path: Path,
) -> None:
    system_root = tmp_path / "system"
    home = tmp_path / "home"
    system_root.mkdir()
    home.mkdir()
    (system_root / ".home").write_text(str(home), encoding="utf-8")

    assert leftovers._installed_state(system_root, "IntelliJIdea2025.1") is True


def test_cleanup_refuses_if_any_jetbrains_process_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    monkeypatch.setattr(leftovers, "clear_jetbrains_process_cache", lambda: None)
    monkeypatch.setattr(leftovers, "jetbrains_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        cleanup_jetbrains_expired_system_directory(_inventory(), {})


def test_cleanup_refuses_tree_change_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    expected = _inventory()
    changed = _inventory(
        stats=JetBrainsTreeStats(
            logical_bytes=expected.stats.logical_bytes + 1,
            entry_count=expected.stats.entry_count,
            latest_write_time_ns=expected.stats.latest_write_time_ns,
        )
    )
    monkeypatch.setattr(leftovers, "_require_no_jetbrains_process", lambda: None)
    monkeypatch.setattr(leftovers, "_inspect_selector", lambda *args, **kwargs: changed)
    monkeypatch.setattr(
        leftovers,
        "purge_exact_directory_tree",
        lambda *args, **kwargs: pytest.fail("must not delete changed tree"),
    )

    environment = {
        "APPDATA": r"C:\Users\test\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
    }
    with pytest.raises(RuntimeError, match="内容在确认后发生变化"):
        cleanup_jetbrains_expired_system_directory(expected, environment)


def test_cleanup_uses_handle_bound_exact_system_root_and_keeps_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    expected = _inventory()
    monkeypatch.setattr(leftovers, "_require_no_jetbrains_process", lambda: None)
    monkeypatch.setattr(leftovers, "_inspect_selector", lambda *args, **kwargs: expected)
    monkeypatch.setattr(leftovers, "is_local_fixed_path", lambda path: True)
    boundary = ExactRootBoundary(
        path=expected.system_root.parent,
        volume_serial=999,
        file_id="parent",
        file_id_kind="128",
    )
    monkeypatch.setattr(leftovers, "_exact_root_boundary", lambda path: boundary)
    calls: list[tuple[Path, ExactDirectorySnapshot, ExactRootBoundary]] = []

    def purge(
        root: Path,
        snapshot: ExactDirectorySnapshot,
        exact_boundary: ExactRootBoundary,
    ) -> DirectoryPurgeResult:
        calls.append((root, snapshot, exact_boundary))
        return DirectoryPurgeResult(
            root_path=str(root),
            files_removed=10,
            links_removed=0,
            directories_removed=3,
            bytes_removed=expected.stats.logical_bytes,
            root_absent=True,
            completed=True,
        )

    monkeypatch.setattr(leftovers, "purge_exact_directory_tree", purge)
    environment = {
        "APPDATA": r"C:\Users\test\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
    }

    result = cleanup_jetbrains_expired_system_directory(expected, environment)

    assert result.root_absent
    assert calls == [(expected.system_root, expected.system_identity, boundary)]
    assert calls[0][0] != expected.config_root


def test_cleanup_refuses_nonexpired_review_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(leftovers, "_WINDOWS", True)
    expected = _inventory()
    expected = JetBrainsLeftoverInventory(
        selector=expected.selector,
        config_root=expected.config_root,
        system_root=expected.system_root,
        config_identity=expected.config_identity,
        system_identity=expected.system_identity,
        stats=expected.stats,
        stale_days=30,
        vendor_expired=False,
        installed=False,
        cleanup_supported=False,
        reason="recent",
    )

    with pytest.raises(ValueError, match="180"):
        cleanup_jetbrains_expired_system_directory(expected, {})
