"""Read-only Go cache inventory plus vendor-supported cleanup operations.

Go exposes two very different cache-cleaning operations. The build cache holds
compiled build artifacts and is a deterministic cleanup candidate when reclaim
is worthwhile. The module cache is shared downloaded dependency source; Go can
safely remove it, but whether keeping those dependencies is valuable depends on
the user's projects and network/offline needs, so DevClean leaves that choice to
the user instead of asking AI.
"""

from __future__ import annotations

import os
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

_GIB = 1024**3
_BUILD_RECOMMEND_BYTES = _GIB


class GoCacheKind(StrEnum):
    BUILD = "build"
    MODULE = "module"


class GoMaintenanceLane(StrEnum):
    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    USER_REVIEW = "USER_REVIEW"


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
                    lane=go_maintenance_lane(kind),
                    recommended=(
                        kind is GoCacheKind.BUILD and size >= _BUILD_RECOMMEND_BYTES
                    ),
                    reason=_decision_reason(kind),
                )
            )
    return GoStorageInventory(tuple(entries))


def go_maintenance_lane(kind: GoCacheKind) -> GoMaintenanceLane:
    if kind is GoCacheKind.BUILD:
        return GoMaintenanceLane.DETERMINISTIC_CANDIDATE
    return GoMaintenanceLane.USER_REVIEW


def clean_go_cache(
    kind: GoCacheKind,
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> GoCacheCleanResult:
    """Delegate one exact audited cache clean to the Go command."""

    clear_go_process_cache()
    expected = _roots_for_kind(kind, environment)
    target = _impl._normalize(path)
    if not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 Go {kind.value} cache 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"Go {kind.value} cache 不存在: {path}")
    if go_process_running():
        raise RuntimeError("Go/gopls 进程正在运行; 请等待完成后再清理")

    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    variable = _override_for_kind(kind)
    env[variable] = str(path)
    executable = go_executable(environment)

    confirmed = _run_go((executable, "env", variable), env, timeout=60)
    if confirmed.returncode != 0:
        detail = _combined_output(confirmed.stdout, confirmed.stderr)
        raise RuntimeError(
            f"go env {variable} 失败 (退出码 {confirmed.returncode}): "
            f"{detail or 'no output'}"
        )
    confirmed_path = _parse_go_env_path(confirmed.stdout)
    if confirmed_path is None or _impl._normalize(confirmed_path) != target:
        raise RuntimeError(f"Go 未确认所选 {kind.value} cache 路径; 已安全停止")

    before = _directory_bytes(path)
    command = (executable, "clean", _flag_for_kind(kind))
    result = _run_go(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"go clean {_flag_for_kind(kind)} 失败 "
            f"(退出码 {result.returncode}): {output or 'no output'}"
        )

    after = _directory_bytes(path)
    return GoCacheCleanResult(
        kind=kind,
        path=path,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


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


def _parse_go_env_path(stdout: str | None) -> Path | None:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    candidate = PureWindowsPath(lines[-1].strip().strip('"').strip("'"))
    return Path(str(candidate)) if candidate.is_absolute() else None


def _roots_for_kind(
    kind: GoCacheKind,
    environment: Mapping[str, str] | None,
) -> tuple[PureWindowsPath, ...]:
    roots = go_roots(environment)
    return {
        GoCacheKind.BUILD: roots.build_cache_roots,
        GoCacheKind.MODULE: roots.module_cache_roots,
    }[kind]


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


def _decision_reason(kind: GoCacheKind) -> str:
    if kind is GoCacheKind.BUILD:
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


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()
    )


__all__ = [
    "GoCacheCleanResult",
    "GoCacheEntry",
    "GoCacheKind",
    "GoMaintenanceLane",
    "GoStorageInventory",
    "clean_go_cache",
    "go_maintenance_lane",
    "inventory_go_storage",
]
