"""Exact per-drive Windows Recycle Bin inventory and user-reviewed emptying."""

# ruff: noqa: RUF001

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

from devclean.platform.windows.volumes import DriveType, drive_type, fixed_volume_roots

_WINDOWS = os.name == "nt"
_SHERB_NOCONFIRMATION = 0x00000001
_SHERB_NOPROGRESSUI = 0x00000002


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


@dataclass(frozen=True, slots=True)
class RecycleBinDriveInventory:
    root: Path
    logical_bytes: int
    item_count: int
    cleanup_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RecycleBinCleanupResult:
    before: RecycleBinDriveInventory
    after: RecycleBinDriveInventory

    @property
    def reported_bytes_removed(self) -> int:
        """Return only the Shell-reported logical-size delta."""

        return max(0, self.before.logical_bytes - self.after.logical_bytes)


def inventory_windows_recycle_bins() -> tuple[RecycleBinDriveInventory, ...]:
    """Read current-user Recycle Bin accounting for each fixed local drive."""

    if not _WINDOWS:
        raise RuntimeError("Windows 回收站维护仅支持 Windows")

    inventories: list[RecycleBinDriveInventory] = []
    for root in fixed_volume_roots():
        exact_root = _validated_fixed_drive_root(root)
        try:
            logical_bytes, item_count = _query_recycle_bin(exact_root)
        except RuntimeError as error:
            inventories.append(
                RecycleBinDriveInventory(
                    root=exact_root,
                    logical_bytes=0,
                    item_count=0,
                    cleanup_supported=False,
                    reason=f"Windows Shell 无法读取该驱动器回收站：{error}",
                )
            )
            continue
        inventories.append(
            RecycleBinDriveInventory(
                root=exact_root,
                logical_bytes=logical_bytes,
                item_count=item_count,
                cleanup_supported=item_count > 0,
                reason=(
                    "回收站中的文件是用户主动删除后仍可恢复的数据；清空会永久删除，必须由用户确认"
                    if item_count > 0
                    else "该驱动器的当前用户回收站为空"
                ),
            )
        )
    return tuple(inventories)


def empty_windows_recycle_bin(
    expected: RecycleBinDriveInventory,
) -> RecycleBinCleanupResult:
    """Empty exactly one fixed drive's Recycle Bin after a fresh USER_REVIEW check."""

    if not _WINDOWS:
        raise RuntimeError("Windows 回收站维护仅支持 Windows")
    if not expected.cleanup_supported or expected.item_count <= 0:
        raise ValueError("用户确认的回收站检查结果没有可执行的清空操作")

    root = _validated_fixed_drive_root(expected.root)
    if root != expected.root:
        raise RuntimeError("回收站驱动器边界已变化；请重新检查")

    current = _inventory_one(root)
    if current.logical_bytes != expected.logical_bytes or current.item_count != expected.item_count:
        raise RuntimeError("回收站内容在确认后发生变化；请重新检查并再次确认")

    _empty_recycle_bin(root)
    after = _inventory_one(root)
    if after.item_count != 0 or after.logical_bytes != 0:
        raise RuntimeError("Windows Shell 已返回，但回收站仍非空；不报告清空成功")
    return RecycleBinCleanupResult(before=current, after=after)


def _inventory_one(root: Path) -> RecycleBinDriveInventory:
    exact_root = _validated_fixed_drive_root(root)
    logical_bytes, item_count = _query_recycle_bin(exact_root)
    return RecycleBinDriveInventory(
        root=exact_root,
        logical_bytes=logical_bytes,
        item_count=item_count,
        cleanup_supported=item_count > 0,
        reason=(
            "回收站中的文件是用户主动删除后仍可恢复的数据；清空会永久删除，必须由用户确认"
            if item_count > 0
            else "该驱动器的当前用户回收站为空"
        ),
    )


def _validated_fixed_drive_root(root: Path) -> Path:
    candidate = Path(os.path.abspath(root))
    anchor = candidate.anchor
    if not anchor:
        raise ValueError(f"回收站目标不是驱动器路径: {root}")
    exact_root = Path(anchor)
    if candidate != exact_root:
        raise ValueError(f"只允许精确驱动器根目录，不接受子路径: {root}")
    if drive_type(exact_root) is not DriveType.FIXED:
        raise ValueError(f"只允许本地固定磁盘回收站: {exact_root}")
    return exact_root


def _shell32() -> ctypes.WinDLL:
    system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    path = Path(system_root) / "System32" / "shell32.dll"
    try:
        return ctypes.WinDLL(str(path), use_last_error=True)
    except OSError as error:
        raise RuntimeError(f"无法加载 Windows Shell API: {path}") from error


def _query_recycle_bin(root: Path) -> tuple[int, int]:
    shell32 = _shell32()
    query = shell32.SHQueryRecycleBinW
    query.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(_SHQUERYRBINFO))
    query.restype = ctypes.c_long
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
    result = int(query(str(root), ctypes.byref(info)))
    if result != 0:
        raise RuntimeError(f"SHQueryRecycleBinW 失败 (HRESULT 0x{result & 0xFFFFFFFF:08X})")
    if info.i64Size < 0 or info.i64NumItems < 0:
        raise RuntimeError("SHQueryRecycleBinW 返回了无效的负数统计")
    return int(info.i64Size), int(info.i64NumItems)


def _empty_recycle_bin(root: Path) -> None:
    shell32 = _shell32()
    empty = shell32.SHEmptyRecycleBinW
    empty.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32)
    empty.restype = ctypes.c_long
    # DevClean has already shown an explicit irreversible USER_REVIEW dialog.
    # Suppress only the duplicate Shell confirmation/progress UI. Never pass
    # NULL/empty root: Microsoft documents that as emptying every drive.
    flags = _SHERB_NOCONFIRMATION | _SHERB_NOPROGRESSUI
    result = int(empty(None, str(root), flags))
    if result != 0:
        raise RuntimeError(f"SHEmptyRecycleBinW 失败 (HRESULT 0x{result & 0xFFFFFFFF:08X})")


__all__ = [
    "RecycleBinCleanupResult",
    "RecycleBinDriveInventory",
    "empty_windows_recycle_bin",
    "inventory_windows_recycle_bins",
]
