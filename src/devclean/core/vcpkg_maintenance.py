"""Source-audited vcpkg storage inventory and user-directed maintenance."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.platform.windows.exact_cleanup import (
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_exact_directory_tree,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path


class VcpkgStorageKind(StrEnum):
    PACKAGES = "packages"
    BUILDTREES = "buildtrees"
    DOWNLOADS = "downloads"
    DEFAULT_BINARY_CACHE = "default-binary-cache"


@dataclass(frozen=True, slots=True)
class VcpkgStorageEntry:
    kind: VcpkgStorageKind
    path: Path
    logical_bytes: int
    exists: bool
    executable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class VcpkgStorageInventory:
    root: Path
    executable: Path
    version: str
    entries: tuple[VcpkgStorageEntry, ...]


@dataclass(frozen=True, slots=True)
class VcpkgCleanResult:
    kind: VcpkgStorageKind
    path: Path
    before_bytes: int
    after_bytes: int

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_vcpkg_root(root: Path) -> VcpkgStorageInventory:
    """Validate one vcpkg instance and inventory only source-backed storage."""

    validated = _validated_root(root)
    executable = _vcpkg_executable(validated)
    version = _vcpkg_version(executable, validated)
    entries = [
        _entry(VcpkgStorageKind.PACKAGES, validated / "packages"),
        _entry(VcpkgStorageKind.BUILDTREES, validated / "buildtrees"),
        _entry(VcpkgStorageKind.DOWNLOADS, validated / "downloads"),
    ]
    binary_cache = _default_binary_cache()
    if binary_cache is not None:
        entries.append(
            VcpkgStorageEntry(
                kind=VcpkgStorageKind.DEFAULT_BINARY_CACHE,
                path=binary_cache,
                logical_bytes=_directory_bytes(binary_cache) if binary_cache.is_dir() else 0,
                exists=binary_cache.is_dir(),
                executable=False,
                reason=(
                    "vcpkg 默认二进制缓存：可重新生成，但会换来后续重新编译成本；"
                    "当前版本仅报告，不直接删除"
                ),
            )
        )
    return VcpkgStorageInventory(validated, executable, version, tuple(entries))


def clean_vcpkg_storage(root: Path, kind: VcpkgStorageKind) -> VcpkgCleanResult:
    """Delete one exact root-local vcpkg temporary tree after fresh validation."""

    if kind is VcpkgStorageKind.DEFAULT_BINARY_CACHE:
        raise ValueError("默认二进制缓存当前仅报告，不授予直接删除权限")
    validated = _validated_root(root)
    executable = _vcpkg_executable(validated)
    _vcpkg_version(executable, validated)
    if vcpkg_activity_running():
        raise RuntimeError("vcpkg/CMake/Ninja/MSBuild 构建活动仍在运行；请结束后再清理")
    target = validated / kind.value
    _validate_target(validated, target)
    if not is_local_fixed_path(validated) or not is_local_fixed_path(target):
        raise RuntimeError("vcpkg 清理仅允许本机固定磁盘；共享、远程或可移动存储只报告")

    boundary = _exact_root_boundary(validated)
    expected = _exact_directory_snapshot(target, f"vcpkg {kind.value}")
    before = _directory_bytes(target)
    result = purge_exact_directory_tree(target, expected, boundary)
    if not result.completed or not result.root_absent:
        raise RuntimeError(f"vcpkg {kind.value} 精确删除未完整完成")
    after = _directory_bytes(target) if target.is_dir() else 0
    return VcpkgCleanResult(kind, target, before, after)


def vcpkg_activity_running() -> bool:
    """Fail closed while common vcpkg build clients are active on Windows."""

    if os.name != "nt":
        return False
    names = "vcpkg|cmake|ninja|msbuild|cl|link"
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        f"$_.ProcessName -match '^({names})$' }}; if ($p) {{ 'RUNNING' }}"
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


def _validated_root(root: Path) -> Path:
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 vcpkg 根目录: {root}") from error
    if not resolved.is_dir():
        raise ValueError(f"vcpkg 根目录不存在: {resolved}")
    if not (resolved / ".vcpkg-root").is_file():
        raise ValueError("所选目录不是可确认的 vcpkg 根目录：缺少 .vcpkg-root")
    executable = _vcpkg_executable(resolved)
    if not executable.is_file():
        raise ValueError(f"vcpkg 可执行文件不存在: {executable}")
    return resolved


def _vcpkg_executable(root: Path) -> Path:
    return root / ("vcpkg.exe" if os.name == "nt" else "vcpkg")


def _vcpkg_version(executable: Path, root: Path) -> str:
    try:
        result = subprocess.run(
            [str(executable), "version", f"--vcpkg-root={root}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 vcpkg version: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"vcpkg 无法确认该根目录 (退出码 {result.returncode}): {detail}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else "unknown"


def _entry(kind: VcpkgStorageKind, path: Path) -> VcpkgStorageEntry:
    exists = path.is_dir()
    reason = {
        VcpkgStorageKind.PACKAGES: (
            "构建后的包暂存目录；官方说明只关心已安装包时可删除，但是否保留诊断/检查状态由你决定"
        ),
        VcpkgStorageKind.BUILDTREES: (
            "构建树通常可重建，但 --editable 会故意保留可修改源码；这里可能存在未提交的人工作业"
        ),
        VcpkgStorageKind.DOWNLOADS: (
            "下载的源码和工具可重新获取，但删除会损失离线/弱网下的复用价值"
        ),
    }[kind]
    return VcpkgStorageEntry(
        kind=kind,
        path=path,
        logical_bytes=_directory_bytes(path) if exists else 0,
        exists=exists,
        executable=exists,
        reason=reason,
    )


def _default_binary_cache() -> Path | None:
    configured = os.environ.get("VCPKG_DEFAULT_BINARY_CACHE")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else None
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "vcpkg" / "archives"
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "vcpkg" / "archives"
    return None


def _validate_target(root: Path, target: Path) -> None:
    expected = root / target.name
    if target != expected or target.name not in {"packages", "buildtrees", "downloads"}:
        raise ValueError(f"不是已审计的 vcpkg 根目录直接子树: {target}")
    if not target.exists():
        raise FileNotFoundError(f"vcpkg {target.name} 不存在: {target}")
    if target.is_symlink() or target.is_junction() or not target.is_dir():
        raise ValueError(f"拒绝清理链接/junction/非目录形式的 vcpkg {target.name}: {target}")


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
    snapshot = _exact_directory_snapshot(path, "vcpkg 根目录")
    return ExactRootBoundary(
        path=path,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


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
    "VcpkgCleanResult",
    "VcpkgStorageEntry",
    "VcpkgStorageInventory",
    "VcpkgStorageKind",
    "clean_vcpkg_storage",
    "inspect_vcpkg_root",
    "vcpkg_activity_running",
]
