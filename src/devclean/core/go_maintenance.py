"""Read-only Go cache inventory plus source-bounded vendor cleanup operations.

Go exposes two very different cache-cleaning operations. The build cache holds
compiled build artifacts and is a deterministic cleanup candidate when the
ordinary local disk cache is in use. The module cache is shared downloaded
dependency source; Go can safely remove it, but whether keeping those dependencies
is valuable depends on the user's projects and network/offline needs, so DevClean
leaves that choice to the user instead of asking AI.

Mutation is delegated to one exact Go executable and one exact cache root. The
final ``go clean`` command pins every destructive clean boolean so inherited or
persisted ``GOFLAGS`` cannot silently widen a build-cache clean into module,
fuzz, package-output, or recursive cleanup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.go_cleanup import (
    clear_go_process_cache,
    go_executable,
    go_process_running,
    go_roots,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_GIB = 1024**3
_BUILD_RECOMMEND_BYTES = _GIB


class GoCacheKind(StrEnum):
    BUILD = "build"
    MODULE = "module"


class GoMaintenanceLane(StrEnum):
    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    USER_REVIEW = "USER_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"


@dataclass(frozen=True, slots=True)
class GoPathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    is_directory: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class GoCacheEntry:
    kind: GoCacheKind
    path: Path
    logical_bytes: int
    exists: bool
    lane: GoMaintenanceLane
    recommended: bool
    reason: str


@dataclass(frozen=True, slots=True)
class GoStorageInventory:
    caches: tuple[GoCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches)

    @property
    def deterministic_bytes(self) -> int:
        return sum(
            entry.logical_bytes
            for entry in self.caches
            if entry.lane is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
        )

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches if entry.recommended)


@dataclass(frozen=True, slots=True)
class GoCacheCleanResult:
    kind: GoCacheKind
    path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_go_storage(
    environment: Mapping[str, str] | None = None,
) -> GoStorageInventory:
    """Inventory exact effective Go cache roots without mutation."""

    roots = go_roots(environment)
    pairs = (
        (GoCacheKind.BUILD, roots.build_cache_roots),
        (GoCacheKind.MODULE, roots.module_cache_roots),
    )
    entries: list[GoCacheEntry] = []
    seen: set[tuple[GoCacheKind, str]] = set()
    for kind, candidates in pairs:
        cache_program = roots.build_cache_program if kind is GoCacheKind.BUILD else ""
        lane = go_maintenance_lane(kind, cache_program)
        for raw in candidates:
            path = Path(str(raw))
            key = (kind, _impl._normalize(path))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            try:
                exists = path.is_dir()
            except OSError:
                exists = False
            size = _directory_bytes(path) if exists else 0
            entries.append(
                GoCacheEntry(
                    kind=kind,
                    path=path,
                    logical_bytes=size,
                    exists=exists,
                    lane=lane,
                    recommended=(
                        kind is GoCacheKind.BUILD
                        and lane is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
                        and size >= _BUILD_RECOMMEND_BYTES
                    ),
                    reason=_decision_reason(kind, cache_program),
                )
            )
    return GoStorageInventory(tuple(entries))


def go_maintenance_lane(
    kind: GoCacheKind,
    build_cache_program: str = "",
) -> GoMaintenanceLane:
    if kind is GoCacheKind.BUILD:
        if build_cache_program.strip():
            return GoMaintenanceLane.REPORT_ONLY
        return GoMaintenanceLane.DETERMINISTIC_CANDIDATE
    return GoMaintenanceLane.USER_REVIEW


def clean_go_cache(
    kind: GoCacheKind,
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> GoCacheCleanResult:
    """Delegate one exact audited cache clean to one identity-bound Go command."""

    clear_go_process_cache()
    roots = go_roots(environment)
    if kind is GoCacheKind.BUILD and roots.build_cache_program:
        raise RuntimeError(
            "GOCACHEPROG 已配置外部 Go build cache; 本地 build cache 仅报告,不自动清理"
        )

    expected = {
        GoCacheKind.BUILD: roots.build_cache_roots,
        GoCacheKind.MODULE: roots.module_cache_roots,
    }[kind]
    target = _impl._normalize(path)
    if not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 Go {kind.value} cache 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"Go {kind.value} cache 不存在: {path}")

    _require_process_idle()
    tool = _resolve_go_tool(environment)
    reviewed_cache = _path_identity(
        path,
        expect_directory=True,
        label=f"Go {kind.value} cache",
    )
    env = _merged_environment(environment)
    variable = _override_for_kind(kind)
    env[variable] = str(reviewed_cache.path)

    _confirm_go_config(tool, kind, env, target)

    # Revalidate every mutable identity immediately before invoking go clean.
    # The final command pins every destructive boolean after GOFLAGS is applied,
    # so persistent/user GOFLAGS cannot widen the reviewed action.
    _require_process_idle()
    if _path_identity(tool.path, expect_directory=False, label="Go CLI") != tool:
        raise RuntimeError("Go CLI 身份在执行前发生变化; 请重新扫描")
    if (
        _path_identity(
            reviewed_cache.path,
            expect_directory=True,
            label=f"Go {kind.value} cache",
        )
        != reviewed_cache
    ):
        raise RuntimeError(f"Go {kind.value} cache 身份在执行前发生变化; 请重新扫描")
    _confirm_go_config(tool, kind, env, target)

    before = _directory_bytes(reviewed_cache.path)
    command = (str(tool.path), *_clean_args(kind))
    result = _run_go(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"go clean {_flag_for_kind(kind)} 失败 "
            f"(退出码 {result.returncode}): {output or 'no output'}"
        )

    if _path_identity(tool.path, expect_directory=False, label="Go CLI") != tool:
        raise RuntimeError("Go CLI 身份在清理后发生变化; 不报告成功")
    _verify_cache_postcondition(kind, reviewed_cache)
    after = _directory_bytes(reviewed_cache.path)
    return GoCacheCleanResult(
        kind=kind,
        path=reviewed_cache.path,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _confirm_go_config(
    tool: GoPathIdentity,
    kind: GoCacheKind,
    environment: dict[str, str],
    target: str,
) -> None:
    command = (
        str(tool.path),
        "env",
        "-json",
        "GOCACHE",
        "GOCACHEPROG",
        "GOMODCACHE",
    )
    confirmed = _run_go(command, environment, timeout=60)
    if confirmed.returncode != 0:
        detail = _combined_output(confirmed.stdout, confirmed.stderr)
        raise RuntimeError(
            "go env -json cache configuration 失败 "
            f"(退出码 {confirmed.returncode}): {detail or 'no output'}"
        )
    try:
        payload = json.loads(confirmed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("go env -json cache configuration 返回无效 JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("go env -json cache configuration 未返回对象")

    variable = _override_for_kind(kind)
    raw_path = payload.get(variable)
    if not isinstance(raw_path, str):
        raise RuntimeError(f"go env 未返回字符串 {variable}; 已安全停止")
    confirmed_path = _parse_go_env_path(raw_path)
    if confirmed_path is None or _impl._normalize(confirmed_path) != target:
        raise RuntimeError(f"Go 未确认所选 {kind.value} cache 路径; 已安全停止")

    if kind is GoCacheKind.BUILD:
        raw_program = payload.get("GOCACHEPROG")
        if not isinstance(raw_program, str):
            raise RuntimeError("go env 未返回字符串 GOCACHEPROG; 已安全停止")
        if raw_program.strip():
            raise RuntimeError(
                "GOCACHEPROG 已配置外部 Go build cache; 本地 build cache 仅报告,不自动清理"
            )


def _clean_args(kind: GoCacheKind) -> tuple[str, ...]:
    return (
        "clean",
        "-i=false",
        "-r=false",
        f"-cache={'true' if kind is GoCacheKind.BUILD else 'false'}",
        "-testcache=false",
        f"-modcache={'true' if kind is GoCacheKind.MODULE else 'false'}",
        "-fuzzcache=false",
        "-n=false",
    )


def _verify_cache_postcondition(kind: GoCacheKind, reviewed: GoPathIdentity) -> None:
    try:
        exists = reviewed.path.exists()
    except OSError as error:
        raise RuntimeError(f"无法确认 Go {kind.value} cache 清理后状态: {error}") from error

    # go clean -cache preserves the top GOCACHE directory; -modcache may remove
    # GOMODCACHE itself. If a module-cache path still exists, it must still be
    # the exact object reviewed before mutation rather than a replacement.
    if kind is GoCacheKind.BUILD and not exists:
        raise RuntimeError("go clean -cache 后 GOCACHE 根目录消失; 不报告成功")
    if not exists:
        return
    current = _path_identity(
        reviewed.path,
        expect_directory=True,
        label=f"Go {kind.value} cache",
    )
    if current != reviewed:
        raise RuntimeError(f"Go {kind.value} cache 身份在清理后发生变化; 不报告成功")


def _resolve_go_tool(environment: Mapping[str, str] | None) -> GoPathIdentity:
    env = _merged_environment(environment)
    raw = go_executable(environment)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        resolved = shutil.which(raw, path=_environment_value(env, "PATH"))
        if resolved is None:
            raise FileNotFoundError("未找到 Go CLI")
        candidate = Path(resolved)
    return _path_identity(candidate, expect_directory=False, label="Go CLI")


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> GoPathIdentity:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or candidate.is_junction():
        raise RuntimeError(f"{label} 不能是 symlink/junction/reparse")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"无法解析 {label}: {error}") from error
    if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(
        os.path.abspath(resolved)
    ):
        raise RuntimeError(f"{label} 路径包含重定向/reparse")
    if not is_local_fixed_path(resolved):
        raise RuntimeError(f"{label} 不在本地固定磁盘")
    try:
        metadata = read_file_metadata(resolved)
    except OSError as error:
        raise RuntimeError(f"无法读取 {label} 文件系统身份: {error}") from error
    if metadata.is_directory != expect_directory:
        raise RuntimeError(f"{label} 类型与预期不一致")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise RuntimeError(f"{label} 是 reparse/cloud placeholder; 不授予维护权限")
    if (
        metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError(f"{label} 缺少稳定文件系统身份")
    return GoPathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        is_directory=metadata.is_directory,
        creation_time_ns=None if expect_directory else metadata.creation_time_ns,
        last_write_time_ns=None if expect_directory else metadata.last_write_time_ns,
    )


def _require_process_idle() -> None:
    clear_go_process_cache()
    if go_process_running():
        raise RuntimeError("Go/gopls 进程正在运行; 请等待完成后再清理")


def _run_go(
    command: tuple[str, ...],
    environment: dict[str, str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Go 命令: {error}") from error


def _parse_go_env_path(value: str | None) -> Path | None:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    candidate = PureWindowsPath(raw)
    if candidate.is_absolute():
        return Path(str(candidate))
    native = Path(raw)
    return Path(os.path.abspath(native)) if native.is_absolute() else None


def _override_for_kind(kind: GoCacheKind) -> str:
    return {
        GoCacheKind.BUILD: "GOCACHE",
        GoCacheKind.MODULE: "GOMODCACHE",
    }[kind]


def _flag_for_kind(kind: GoCacheKind) -> str:
    return {
        GoCacheKind.BUILD: "-cache",
        GoCacheKind.MODULE: "-modcache",
    }[kind]


def _decision_reason(kind: GoCacheKind, build_cache_program: str = "") -> str:
    if kind is GoCacheKind.BUILD:
        if build_cache_program.strip():
            return (
                "Go 已配置 GOCACHEPROG 外部构建缓存后端; 本地 GOCACHE 仅报告,"
                "不授予自动维护权限"
            )
        return "Go 编译构建缓存; 清理后只是后续构建重新编译, 不需要 AI 判断"
    return "共享的已下载模块源码; Go 可安全清空, 但离线/旧项目是否仍需要由你决定"


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


def _merged_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    return env


def _environment_value(environment: Mapping[str, str], key: str) -> str | None:
    folded = key.casefold()
    for name, value in environment.items():
        if name.casefold() == folded and value:
            return value
    return None


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()
    )


__all__ = [
    "GoCacheCleanResult",
    "GoCacheEntry",
    "GoCacheKind",
    "GoMaintenanceLane",
    "GoPathIdentity",
    "GoStorageInventory",
    "clean_go_cache",
    "go_maintenance_lane",
    "inventory_go_storage",
]
