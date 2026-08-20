"""Exact Meson configured build-directory inventory and user-directed removal."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devclean.platform.windows.exact_cleanup import (
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_exact_directory_tree,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_GIB = 1024**3
_REVIEW_BYTES = 2 * _GIB
_MESON_BUILD_FILENAMES = frozenset({"meson.build", "meson.options", "meson_options.txt"})


@dataclass(frozen=True, slots=True)
class MesonBuildInventory:
    source_root: Path
    build_root: Path
    logical_bytes: int
    executable: str
    version: str
    buildsystem_files: tuple[Path, ...]
    source_identity: ExactDirectorySnapshot
    build_identity: ExactDirectorySnapshot
    deletion_supported: bool
    worth_reviewing: bool
    user_review_required: bool = True


@dataclass(frozen=True, slots=True)
class MesonBuildRemovalResult:
    source_root: Path
    build_root: Path
    before_bytes: int
    after_bytes: int

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_meson_build(
    source_root: Path,
    build_root: Path,
    environment: Mapping[str, str] | None = None,
) -> MesonBuildInventory:
    """Bind one exact configured Meson build tree to its selected source root."""

    source = _validated_source_root(source_root)
    build_dir = _validated_build_root(build_root)
    _require_non_destructive_layout(source, build_dir)

    source_before = _exact_directory_snapshot(source, "Meson 源码根目录")
    build_before = _exact_directory_snapshot(build_dir, "Meson 构建目录")
    executable = meson_executable(environment)
    version = _meson_version(executable, source, environment)
    buildsystem_files = _meson_buildsystem_files(
        executable,
        source,
        build_dir,
        environment,
    )
    _require_source_binding(source, buildsystem_files)
    source_after = _exact_directory_snapshot(source, "Meson 源码根目录")
    build_after = _exact_directory_snapshot(build_dir, "Meson 构建目录")
    if source_before != source_after or build_before != build_after:
        raise RuntimeError("Meson 源码/构建目录身份在检查期间发生变化；请重新检查")

    logical_bytes = _directory_bytes(build_dir)
    deletion_supported = _safe_local_layout(source, build_dir)
    return MesonBuildInventory(
        source_root=source,
        build_root=build_dir,
        logical_bytes=logical_bytes,
        executable=executable,
        version=version,
        buildsystem_files=buildsystem_files,
        source_identity=source_after,
        build_identity=build_after,
        deletion_supported=deletion_supported,
        worth_reviewing=logical_bytes >= _REVIEW_BYTES,
    )


def remove_meson_build_directory(
    source_root: Path,
    build_root: Path,
    environment: Mapping[str, str] | None = None,
) -> MesonBuildRemovalResult:
    """Remove one exact configured Meson build tree after fresh identity proof."""

    initial = inspect_meson_build(source_root, build_root, environment)
    if not initial.deletion_supported:
        raise ValueError(
            "Meson 构建目录不在可批准的本地固定磁盘边界内；共享、远程、可移动或重定向存储只允许检查"
        )
    if meson_build_process_running():
        raise RuntimeError("Meson 或构建后端/编译器正在运行；请结束相关构建后再删除构建目录")

    # Re-run Meson's own read-only introspection immediately before mutation.
    # The semantic identity (tool version + configured build-system file set)
    # must remain unchanged; size may legitimately change and is not identity.
    current = inspect_meson_build(source_root, build_root, environment)
    if not current.deletion_supported:
        raise ValueError("Meson 构建目录边界已变化；请重新检查后再操作")
    if (
        current.source_root != initial.source_root
        or current.build_root != initial.build_root
        or current.executable != initial.executable
        or current.version != initial.version
        or current.buildsystem_files != initial.buildsystem_files
    ):
        raise RuntimeError("Meson 配置身份已变化；拒绝继续删除")

    source_now = _exact_directory_snapshot(current.source_root, "Meson 源码根目录")
    build_now = _exact_directory_snapshot(current.build_root, "Meson 构建目录")
    if source_now != current.source_identity or build_now != current.build_identity:
        raise RuntimeError("Meson 源码/构建目录身份在执行前发生变化；拒绝删除")

    boundary_path = current.build_root.parent
    if boundary_path == current.build_root:
        raise ValueError("拒绝把文件系统根目录作为 Meson 构建目录")
    if not is_local_fixed_path(boundary_path):
        raise ValueError("Meson 构建目录父边界不是本地固定磁盘；拒绝删除")
    boundary = _exact_root_boundary(boundary_path)

    before = current.logical_bytes
    result = purge_exact_directory_tree(
        current.build_root,
        current.build_identity,
        boundary,
    )
    if not result.completed or not result.root_absent:
        raise RuntimeError("Meson 构建目录精确删除未完整完成")
    after = _directory_bytes(current.build_root) if _is_directory(current.build_root) else 0
    return MesonBuildRemovalResult(
        source_root=current.source_root,
        build_root=current.build_root,
        before_bytes=before,
        after_bytes=after,
    )


def meson_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_meson_exe")
    if configured:
        return configured
    if environment is None:
        for name in ("meson.exe", "meson") if os.name == "nt" else ("meson",):
            located = shutil.which(name)
            if located:
                return located
    return "meson.exe" if os.name == "nt" else "meson"


def meson_build_process_running() -> bool:
    """Fail closed on Windows when Meson/build backend/compiler activity is visible."""

    if os.name != "nt":
        return False
    script = r"""
$ErrorActionPreference = 'Stop'
$names = @(
  'ninja','samu','msbuild','devenv','cl','clang','clang-cl',
  'gcc','g++','cc','c++','link','lld-link'
)
try {
  $processes = Get-CimInstance Win32_Process
  foreach ($p in $processes) {
    $name = [System.IO.Path]::GetFileNameWithoutExtension([string]$p.Name).ToLowerInvariant()
    if ($names -contains $name) { 'RUNNING'; exit 0 }
    if ($name -eq 'meson') { 'RUNNING'; exit 0 }
    if ($name -in @('python','pythonw','py')) {
      $cmd = [string]$p.CommandLine
      $moduleMatch = $cmd -match '(?i)-m\s+mesonbuild\.mesonmain'
      $scriptMatch = $cmd -match '(?i)(^|[\\/\s\"])(meson|meson\.py)([\s\"]|$)'
      if ($moduleMatch -or $scriptMatch) {
        'RUNNING'; exit 0
      }
    }
  }
  'IDLE'
  exit 0
} catch {
  exit 2
}
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


def _validated_source_root(source_root: Path) -> Path:
    root = _ordinary_resolved_directory(source_root, "Meson 源码根目录")
    if not (root / "meson.build").is_file():
        raise ValueError(f"所选目录没有顶层 meson.build: {root}")
    return root


def _validated_build_root(build_root: Path) -> Path:
    root = _ordinary_resolved_directory(build_root, "Meson 构建目录")
    marker = root / "meson-private" / "coredata.dat"
    if not marker.is_file():
        raise ValueError("所选目录不是已配置的 Meson 构建目录：缺少 meson-private/coredata.dat")
    return root


def _ordinary_resolved_directory(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError(f"{label} 不能是 symlink/junction/reparse 路径: {candidate}")
        if not candidate.is_dir():
            raise ValueError(f"{label} 不存在或不是目录: {candidate}")
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 {label}: {path}") from error
    if _normalized(candidate) != _normalized(resolved):
        raise ValueError(f"{label} 存在路径重定向/reparse；只允许普通本地目录")
    metadata = read_file_metadata(candidate)
    if not metadata.is_directory or metadata.is_reparse_point:
        raise ValueError(f"{label} 不是可验证的普通目录")
    return resolved


def _require_non_destructive_layout(source: Path, build_dir: Path) -> None:
    if _normalized(source) == _normalized(build_dir):
        raise ValueError("Meson 源码目录和构建目录必须不同")
    try:
        common = Path(os.path.commonpath((str(source), str(build_dir))))
    except ValueError as error:
        raise ValueError("无法确认 Meson 源码/构建目录关系") from error
    if _normalized(common) == _normalized(build_dir):
        raise ValueError("拒绝删除包含源码目录的 Meson 构建目录")
    if build_dir.parent == build_dir:
        raise ValueError("拒绝把文件系统根目录作为 Meson 构建目录")


def _safe_local_layout(source: Path, build_dir: Path) -> bool:
    if not (
        is_local_fixed_path(source)
        and is_local_fixed_path(build_dir)
        and is_local_fixed_path(build_dir.parent)
    ):
        return False
    try:
        _exact_root_boundary(build_dir.parent)
    except (RuntimeError, ValueError, OSError):
        return False
    return True


def _meson_version(
    executable: str,
    source: Path,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_meson((executable, "--version"), source, environment, timeout=30)
    version = result.stdout.strip()
    if not version:
        raise RuntimeError("meson --version 没有返回版本")
    return version


def _meson_buildsystem_files(
    executable: str,
    source: Path,
    build_dir: Path,
    environment: Mapping[str, str] | None,
) -> tuple[Path, ...]:
    command = (
        executable,
        "introspect",
        "--buildsystem-files",
        str(build_dir),
    )
    result = _run_meson(command, source, environment, timeout=120)
    try:
        value: Any = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Meson introspect 没有返回有效 JSON") from error
    if not isinstance(value, list) or not value:
        raise RuntimeError("Meson introspect 没有返回 build-system 文件列表")

    files: list[Path] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise RuntimeError("Meson build-system 文件列表包含无效条目")
        path = Path(item).expanduser()
        if not path.is_absolute():
            raise RuntimeError(f"Meson build-system 文件不是绝对路径: {item}")
        files.append(Path(os.path.abspath(path)))
    return tuple(sorted(files, key=lambda item: _normalized(item)))


def _require_source_binding(source: Path, buildsystem_files: tuple[Path, ...]) -> None:
    expected = source / "meson.build"
    normalized_files = {_normalized(path) for path in buildsystem_files}
    if _normalized(expected) not in normalized_files:
        raise ValueError(
            "所选源码根目录与该 Meson 构建目录不匹配：introspect 未报告所选顶层 meson.build"
        )

    # A selected subproject's meson.build also appears in the configured file
    # set. Requiring every canonical Meson build-definition file to remain
    # beneath the selected source root prevents that subproject from being
    # mistaken for the configured top-level source tree. Unusual layouts that
    # intentionally redirect build definitions outside the source fail closed.
    for path in buildsystem_files:
        if path.name.casefold() not in _MESON_BUILD_FILENAMES:
            continue
        if not _is_descendant_or_equal(path, source):
            raise ValueError(
                "Meson introspect 报告了所选源码根目录之外的构建定义；无法证明这是顶层源码根目录"
            )


def _run_meson(
    command: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        result = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Meson CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Meson CLI 失败 (exit {result.returncode}): {detail}")
    return result


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
    snapshot = _exact_directory_snapshot(path, "Meson 构建目录父边界")
    return ExactRootBoundary(
        path=path,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


def _is_descendant_or_equal(path: Path, root: Path) -> bool:
    try:
        common = Path(os.path.commonpath((str(path), str(root))))
    except ValueError:
        return False
    return _normalized(common) == _normalized(root)


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            safe_subdirs: list[str] = []
            for name in subdirs:
                child = base / name
                try:
                    if child.is_symlink() or child.is_junction():
                        continue
                except OSError:
                    continue
                safe_subdirs.append(name)
            subdirs[:] = safe_subdirs
            for name in files:
                path = base / name
                try:
                    if path.is_symlink():
                        continue
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "MesonBuildInventory",
    "MesonBuildRemovalResult",
    "inspect_meson_build",
    "meson_build_process_running",
    "meson_executable",
    "remove_meson_build_directory",
]
