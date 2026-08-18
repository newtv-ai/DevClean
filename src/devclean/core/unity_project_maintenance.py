"""Project-aware Unity Library inventory and user-directed cleanup."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devclean.platform.windows.exact_cleanup import (
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_exact_directory_tree,
)
from devclean.platform.windows.filesystem import read_file_metadata

_GIB = 1024**3
_REVIEW_BYTES = 5 * _GIB


@dataclass(frozen=True, slots=True)
class UnityProjectLibraryInventory:
    project_root: Path
    library: Path
    logical_bytes: int
    exists: bool
    editor_version: str
    worth_reviewing: bool
    user_review_required: bool = True


@dataclass(frozen=True, slots=True)
class UnityLibraryCleanResult:
    project_root: Path
    library: Path
    before_bytes: int
    after_bytes: int

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_unity_project(project_root: Path) -> UnityProjectLibraryInventory:
    """Validate one Unity project and inventory only its direct Library directory."""

    root = _validated_project_root(project_root)
    library = root / "Library"
    exists = _validate_library_entry(library, allow_missing=True)
    size = _directory_bytes(library) if exists else 0
    return UnityProjectLibraryInventory(
        project_root=root,
        library=library,
        logical_bytes=size,
        exists=exists,
        editor_version=_editor_version(root),
        worth_reviewing=exists and size >= _REVIEW_BYTES,
    )


def delete_unity_project_library(project_root: Path) -> UnityLibraryCleanResult:
    """Delete the exact Library directory of a validated, closed Unity project."""

    root = _validated_project_root(project_root)
    library = root / "Library"
    _validate_library_entry(library, allow_missing=False)
    if unity_editor_running():
        raise RuntimeError("Unity Editor 正在运行; 请关闭所有 Unity Editor 后再删除项目 Library")

    # Re-resolve the project boundary immediately before mutation. The target is
    # always the direct child named Library; no filename search or recursive
    # discovery grants deletion authority.
    root = _validated_project_root(root)
    library = root / "Library"
    _validate_library_entry(library, allow_missing=False)
    boundary = _exact_root_boundary(root)
    expected = _exact_directory_snapshot(library, "Unity Library")
    before = _directory_bytes(library)
    result = purge_exact_directory_tree(library, expected, boundary)
    if not result.completed or not result.root_absent:
        raise RuntimeError("Unity Library 精确删除未完整完成")
    after = _directory_bytes(library) if library.is_dir() else 0
    return UnityLibraryCleanResult(
        project_root=root,
        library=library,
        before_bytes=before,
        after_bytes=after,
    )


def unity_editor_running() -> bool:
    """Fail closed on Windows while any Unity Editor process is active."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -Name Unity -ErrorAction SilentlyContinue; "
        "if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def _validated_project_root(project_root: Path) -> Path:
    try:
        root = project_root.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 Unity 项目目录: {project_root}") from error
    if not root.is_dir():
        raise ValueError(f"Unity 项目目录不存在: {root}")
    assets = root / "Assets"
    settings = root / "ProjectSettings"
    version_file = settings / "ProjectVersion.txt"
    if not assets.is_dir() or not settings.is_dir() or not version_file.is_file():
        raise ValueError(
            "所选目录不是可确认的 Unity 项目根目录: 需要 Assets、ProjectSettings/"
            "ProjectVersion.txt"
        )
    return root


def _validate_library_entry(library: Path, *, allow_missing: bool) -> bool:
    try:
        exists = library.exists()
    except OSError as error:
        raise ValueError(f"无法检查 Unity Library: {library}") from error
    if not exists:
        if allow_missing:
            return False
        raise FileNotFoundError(f"Unity Library 不存在: {library}")
    if library.is_symlink() or library.is_junction():
        raise ValueError(f"拒绝删除链接或 junction 形式的 Unity Library: {library}")
    if not library.is_dir():
        raise ValueError(f"Unity Library 不是目录: {library}")
    return True


def _exact_directory_snapshot(path: Path, label: str) -> ExactDirectorySnapshot:
    metadata = read_file_metadata(path)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.creation_time_ns is None
    ):
        raise RuntimeError(f"{label} 没有可验证的普通目录身份")
    return ExactDirectorySnapshot(
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=metadata.creation_time_ns,
    )


def _exact_root_boundary(path: Path) -> ExactRootBoundary:
    snapshot = _exact_directory_snapshot(path, "Unity 项目根目录")
    return ExactRootBoundary(
        path=path,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


def _editor_version(project_root: Path) -> str:
    version_file = project_root / "ProjectSettings" / "ProjectVersion.txt"
    try:
        lines = version_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "unknown"
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key.strip() == "m_EditorVersion":
            version = value.strip()
            if version:
                return version
    return "unknown"


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            base = Path(directory)
            for name in files:
                try:
                    total += (base / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


__all__ = [
    "UnityLibraryCleanResult",
    "UnityProjectLibraryInventory",
    "delete_unity_project_library",
    "inspect_unity_project",
    "unity_editor_running",
]
