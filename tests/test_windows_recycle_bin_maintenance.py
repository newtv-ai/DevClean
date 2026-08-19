from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.windows_recycle_bin_maintenance as recycle_bin
from devclean.core.windows_recycle_bin_maintenance import (
    RecycleBinDriveInventory,
    empty_windows_recycle_bin,
    inventory_windows_recycle_bins,
)
from devclean.platform.windows.volumes import DriveType


def _inventory(
    *,
    root: Path = Path("C:\\"),
    logical_bytes: int = 1024,
    item_count: int = 2,
) -> RecycleBinDriveInventory:
    return RecycleBinDriveInventory(
        root=root,
        logical_bytes=logical_bytes,
        item_count=item_count,
        cleanup_supported=item_count > 0,
        reason="review",
    )


def _fixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recycle_bin, "_WINDOWS", True)
    monkeypatch.setattr(
        recycle_bin,
        "drive_type",
        lambda path: DriveType.FIXED,
    )


def test_inventory_uses_only_fixed_volume_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch)
    monkeypatch.setattr(
        recycle_bin,
        "fixed_volume_roots",
        lambda: (Path("C:\\"), Path("D:\\")),
    )
    values = {
        Path("C:\\"): (4096, 3),
        Path("D:\\"): (0, 0),
    }
    monkeypatch.setattr(
        recycle_bin,
        "_query_recycle_bin",
        lambda root: values[root],
    )

    inventories = inventory_windows_recycle_bins()

    assert [(item.root, item.logical_bytes, item.item_count) for item in inventories] == [
        (Path("C:\\"), 4096, 3),
        (Path("D:\\"), 0, 0),
    ]
    assert inventories[0].cleanup_supported
    assert not inventories[1].cleanup_supported


def test_inventory_query_failure_is_report_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch)
    monkeypatch.setattr(recycle_bin, "fixed_volume_roots", lambda: (Path("C:\\"),))
    monkeypatch.setattr(
        recycle_bin,
        "_query_recycle_bin",
        lambda root: (_ for _ in ()).throw(RuntimeError("denied")),
    )

    inventories = inventory_windows_recycle_bins()

    assert len(inventories) == 1
    assert not inventories[0].cleanup_supported
    assert "denied" in inventories[0].reason


def test_empty_refuses_non_drive_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed(monkeypatch)
    expected = _inventory(root=Path(r"C:\Users"))

    with pytest.raises(ValueError, match="精确驱动器根目录"):
        empty_windows_recycle_bin(expected)


def test_empty_refuses_non_fixed_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recycle_bin, "_WINDOWS", True)
    monkeypatch.setattr(
        recycle_bin,
        "drive_type",
        lambda path: DriveType.REMOVABLE,
    )

    with pytest.raises(ValueError, match="本地固定磁盘"):
        empty_windows_recycle_bin(_inventory())


def test_empty_refuses_when_contents_change_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed(monkeypatch)
    expected = _inventory(logical_bytes=1024, item_count=2)
    monkeypatch.setattr(
        recycle_bin,
        "_query_recycle_bin",
        lambda root: (2048, 3),
    )
    monkeypatch.setattr(
        recycle_bin,
        "_empty_recycle_bin",
        lambda root: pytest.fail("must not empty changed Recycle Bin"),
    )

    with pytest.raises(RuntimeError, match="确认后发生变化"):
        empty_windows_recycle_bin(expected)


def test_empty_targets_exactly_one_reviewed_drive_and_requires_zero_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed(monkeypatch)
    expected = _inventory(root=Path("D:\\"), logical_bytes=8192, item_count=4)
    queries = iter(((8192, 4), (0, 0)))
    emptied: list[Path] = []
    monkeypatch.setattr(
        recycle_bin,
        "_query_recycle_bin",
        lambda root: next(queries),
    )
    monkeypatch.setattr(
        recycle_bin,
        "_empty_recycle_bin",
        lambda root: emptied.append(root),
    )

    result = empty_windows_recycle_bin(expected)

    assert emptied == [Path("D:\\")]
    assert result.before.root == Path("D:\\")
    assert result.before.item_count == 4
    assert result.after.item_count == 0
    assert result.reported_bytes_removed == 8192


def test_empty_does_not_report_success_when_shell_leaves_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed(monkeypatch)
    expected = _inventory(logical_bytes=8192, item_count=4)
    queries = iter(((8192, 4), (1024, 1)))
    monkeypatch.setattr(
        recycle_bin,
        "_query_recycle_bin",
        lambda root: next(queries),
    )
    monkeypatch.setattr(recycle_bin, "_empty_recycle_bin", lambda root: None)

    with pytest.raises(RuntimeError, match="仍非空"):
        empty_windows_recycle_bin(expected)


def test_shell_empty_never_uses_null_all_drive_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, str, int]] = []

    class FakeEmpty:
        argtypes: object = None
        restype: object = None

        def __call__(self, hwnd: object, root: str, flags: int) -> int:
            calls.append((hwnd, root, flags))
            return 0

    class FakeShell:
        SHEmptyRecycleBinW = FakeEmpty()

    monkeypatch.setattr(recycle_bin, "_shell32", lambda: FakeShell())

    recycle_bin._empty_recycle_bin(Path("C:\\"))

    assert calls
    assert calls[0][1] == "C:\\"
    assert calls[0][1] != ""
