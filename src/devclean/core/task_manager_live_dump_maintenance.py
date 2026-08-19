"""Exact Task Manager live-kernel dump inventory and USER_REVIEW removal."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from devclean.platform.windows.exact_cleanup import (
    ExactFileSnapshot,
    ExactMutationResult,
    ExactRootBoundary,
    purge_exact_file,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.known_folders import local_appdata_path
from devclean.platform.windows.volumes import is_local_fixed_path

_WINDOWS = os.name == "nt"
_RELATIVE_ROOT = Path("Microsoft") / "Windows" / "TaskManager" / "LiveKernelDumps"


@dataclass(frozen=True, slots=True)
class TaskManagerLiveDumpEntry:
    path: Path
    root: Path
    logical_bytes: int
    creation_time_ns: int
    last_write_time_ns: int
    deletion_supported: bool
    reason: str
    root_boundary: ExactRootBoundary
    snapshot: ExactFileSnapshot


@dataclass(frozen=True, slots=True)
class TaskManagerLiveDumpInventory:
    root: Path
    entries: tuple[TaskManagerLiveDumpEntry, ...]
    warning: str | None = None

    @property
    def logical_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.entries)


@dataclass(frozen=True, slots=True)
class TaskManagerLiveDumpDeleteResult:
    entry: TaskManagerLiveDumpEntry
    mutation: ExactMutationResult

    @property
    def logical_bytes_removed(self) -> int:
        return self.entry.logical_bytes if self.mutation.source_name_absent else 0


def inventory_task_manager_live_kernel_dumps() -> TaskManagerLiveDumpInventory:
    """Inventory direct `.dmp` children of Task Manager's documented live-kernel root."""

    if not _WINDOWS:
        raise RuntimeError("Task Manager 实时内核转储维护仅支持 Windows")

    root = _absolute(local_appdata_path() / _RELATIVE_ROOT)
    try:
        boundary = _root_boundary(root)
    except FileNotFoundError:
        return TaskManagerLiveDumpInventory(root=root, entries=())
    except (OSError, RuntimeError) as error:
        return TaskManagerLiveDumpInventory(root=root, entries=(), warning=str(error))

    entries: list[TaskManagerLiveDumpEntry] = []
    try:
        with os.scandir(root) as scan:
            for child in scan:
                if not child.name.casefold().endswith(".dmp"):
                    continue
                try:
                    if not child.is_file(follow_symlinks=False):
                        continue
                    snapshot = _file_snapshot(Path(child.path))
                except (OSError, RuntimeError):
                    continue
                entries.append(
                    TaskManagerLiveDumpEntry(
                        path=_absolute(Path(child.path)),
                        root=root,
                        logical_bytes=snapshot.logical_size,
                        creation_time_ns=snapshot.creation_time_ns,
                        last_write_time_ns=snapshot.last_write_time_ns,
                        deletion_supported=True,
                        reason=(
                            "任务管理器实时内核转储是用户主动生成的诊断证据；是否仍需用于 WinDbg/"
                            "驱动故障分析由用户决定"
                        ),
                        root_boundary=boundary,
                        snapshot=snapshot,
                    )
                )
    except OSError as error:
        return TaskManagerLiveDumpInventory(
            root=root,
            entries=(),
            warning=f"无法枚举任务管理器实时内核转储目录: {error}",
        )

    entries.sort(key=lambda item: item.logical_bytes, reverse=True)
    return TaskManagerLiveDumpInventory(root=root, entries=tuple(entries))


def delete_task_manager_live_kernel_dump(
    expected: TaskManagerLiveDumpEntry,
) -> TaskManagerLiveDumpDeleteResult:
    """Delete one reviewed Task Manager live-kernel dump after fresh identity proof."""

    if not _WINDOWS:
        raise RuntimeError("Task Manager 实时内核转储维护仅支持 Windows")
    if not expected.deletion_supported:
        raise ValueError(expected.reason)

    fresh = inventory_task_manager_live_kernel_dumps()
    matches = [
        entry for entry in fresh.entries if _normalized(entry.path) == _normalized(expected.path)
    ]
    if len(matches) != 1:
        raise RuntimeError("无法唯一重新确认所选任务管理器实时内核转储；请重新检查")
    current = matches[0]
    if (
        _normalized(current.root) != _normalized(expected.root)
        or current.root_boundary != expected.root_boundary
        or current.snapshot != expected.snapshot
    ):
        raise RuntimeError("任务管理器实时内核转储的根或文件身份在执行前发生变化；请重新检查")

    mutation = purge_exact_file(current.path, current.snapshot, current.root_boundary)
    if not mutation.source_name_absent:
        raise RuntimeError("精确转储对象已处理，但原路径被并发替换；不能报告清理成功")
    return TaskManagerLiveDumpDeleteResult(entry=current, mutation=mutation)


def _root_boundary(path: Path) -> ExactRootBoundary:
    root = _absolute(path)
    metadata = read_file_metadata(root)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError("任务管理器实时内核转储根没有可验证的普通目录身份")
    if not is_local_fixed_path(root):
        raise RuntimeError("任务管理器实时内核转储根不在本地固定磁盘")
    return ExactRootBoundary(
        path=root,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
    )


def _file_snapshot(path: Path) -> ExactFileSnapshot:
    candidate = _absolute(path)
    metadata = read_file_metadata(candidate)
    if (
        metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.link_count is None
        or metadata.attributes is None
        or metadata.creation_time_ns is None
        or metadata.last_write_time_ns is None
    ):
        raise RuntimeError("任务管理器实时内核转储没有可验证的普通文件身份")
    if metadata.link_count != 1:
        raise RuntimeError("拒绝删除硬链接形式的任务管理器实时内核转储")
    if not is_local_fixed_path(candidate):
        raise RuntimeError("任务管理器实时内核转储不在本地固定磁盘")
    return ExactFileSnapshot(
        logical_size=metadata.logical_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        reparse_tag=metadata.reparse_tag,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "TaskManagerLiveDumpDeleteResult",
    "TaskManagerLiveDumpEntry",
    "TaskManagerLiveDumpInventory",
    "delete_task_manager_live_kernel_dump",
    "inventory_task_manager_live_kernel_dumps",
]
