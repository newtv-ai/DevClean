"""Read-only NuGet local inventory plus vendor-supported clear operations.

The local decision boundary is deliberately narrower than ``dotnet nuget locals
all --clear``. HTTP, temporary, and plugin caches are vendor-defined cache
resources, so DevClean can identify them without AI. The global-packages folder
is different: PackageReference projects consume packages directly from it, so
clearing it is a user decision even though NuGet provides a supported command.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.nuget_cleanup import (
    clear_nuget_process_cache,
    dotnet_executable,
    nuget_process_running,
    nuget_roots,
)

_MIB = 1024**2


class NuGetLocalKind(StrEnum):
    GLOBAL_PACKAGES = "global-packages"
    HTTP_CACHE = "http-cache"
    TEMP = "temp"
    PLUGINS_CACHE = "plugins-cache"


class NuGetMaintenanceLane(StrEnum):
    """Cheap local decisions for NuGet storage; neither lane requires AI."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    USER_REVIEW = "USER_REVIEW"


@dataclass(frozen=True, slots=True)
class NuGetLocalEntry:
    kind: NuGetLocalKind
    path: Path
    logical_bytes: int
    exists: bool
    lane: NuGetMaintenanceLane
    recommended: bool
    reason: str


@dataclass(frozen=True, slots=True)
class NuGetStorageInventory:
    locals: tuple[NuGetLocalEntry, ...]

    @property
    def total_local_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.locals)

    @property
    def deterministic_bytes(self) -> int:
        return sum(
            entry.logical_bytes
            for entry in self.locals
            if entry.lane is NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE
        )

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.locals if entry.recommended)


@dataclass(frozen=True, slots=True)
class NuGetClearResult:
    kind: NuGetLocalKind
    path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_nuget_storage(
    environment: Mapping[str, str] | None = None,
) -> NuGetStorageInventory:
    """Inventory exact effective NuGet local-resource roots without mutation."""

    roots = nuget_roots(environment)
    pairs = (
        (NuGetLocalKind.GLOBAL_PACKAGES, roots.global_packages_roots),
        (NuGetLocalKind.HTTP_CACHE, roots.http_cache_roots),
        (NuGetLocalKind.TEMP, roots.temp_roots),
        (NuGetLocalKind.PLUGINS_CACHE, roots.plugins_cache_roots),
    )
    entries: list[NuGetLocalEntry] = []
    seen: set[tuple[NuGetLocalKind, str]] = set()
    for kind, candidates in pairs:
        for raw in candidates:
            path = Path(str(raw))
            key = (kind, os.path.normcase(os.path.normpath(str(path))))
            if key in seen:
                continue
            seen.add(key)
            try:
                exists = path.is_dir()
            except OSError:
                exists = False
            logical_bytes = _directory_bytes(path) if exists else 0
            lane = nuget_maintenance_lane(kind)
            entries.append(
                NuGetLocalEntry(
                    kind=kind,
                    path=path,
                    logical_bytes=logical_bytes,
                    exists=exists,
                    lane=lane,
                    recommended=_recommended(kind, logical_bytes),
                    reason=_decision_reason(kind),
                )
            )
    return NuGetStorageInventory(tuple(entries))


def nuget_maintenance_lane(kind: NuGetLocalKind) -> NuGetMaintenanceLane:
    """Return the stable local review lane for one documented NuGet resource."""

    if kind is NuGetLocalKind.GLOBAL_PACKAGES:
        return NuGetMaintenanceLane.USER_REVIEW
    return NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE


def clear_nuget_local(
    kind: NuGetLocalKind,
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> NuGetClearResult:
    """Delegate one exact audited local-resource clear to the .NET CLI."""

    clear_nuget_process_cache()
    expected = _roots_for_kind(kind, environment)
    target = _impl._normalize(path)
    if not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 NuGet {kind.value} 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"NuGet {kind.value} 不存在: {path}")
    if nuget_process_running():
        raise RuntimeError("NuGet/.NET restore 或构建进程正在运行; 请等待完成后再清理")

    before = _directory_bytes(path)
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env[_override_for_kind(kind)] = str(path)
    command = [
        dotnet_executable(environment),
        "nuget",
        "locals",
        kind.value,
        "--clear",
        "--force-english-output",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 dotnet nuget locals: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"dotnet nuget locals {kind.value} --clear 失败 "
            f"(退出码 {result.returncode}): {detail}"
        )

    after = _directory_bytes(path)
    return NuGetClearResult(
        kind=kind,
        path=path,
        before_bytes=before,
        after_bytes=after,
        stdout=result.stdout.strip(),
    )


def _recommended(kind: NuGetLocalKind, logical_bytes: int) -> bool:
    """Select only clearly worthwhile deterministic cache work by default."""

    thresholds = {
        NuGetLocalKind.HTTP_CACHE: 64 * _MIB,
        NuGetLocalKind.TEMP: 16 * _MIB,
        NuGetLocalKind.PLUGINS_CACHE: 16 * _MIB,
    }
    threshold = thresholds.get(kind)
    return threshold is not None and logical_bytes >= threshold


def _decision_reason(kind: NuGetLocalKind) -> str:
    if kind is NuGetLocalKind.GLOBAL_PACKAGES:
        return "项目可直接使用这里的已还原依赖; 清空后需要重新 restore, 是否释放由你决定"
    if kind is NuGetLocalKind.HTTP_CACHE:
        return "NuGet 官方 HTTP 请求缓存; 可通过 dotnet nuget locals 安全清空"
    if kind is NuGetLocalKind.TEMP:
        return "NuGet 官方临时缓存; 关闭还原/构建进程后可通过官方命令清空"
    return "NuGet 官方插件操作声明缓存; 可通过 dotnet nuget locals 安全清空"


def _roots_for_kind(
    kind: NuGetLocalKind,
    environment: Mapping[str, str] | None,
) -> tuple[PureWindowsPath, ...]:
    roots = nuget_roots(environment)
    return {
        NuGetLocalKind.GLOBAL_PACKAGES: roots.global_packages_roots,
        NuGetLocalKind.HTTP_CACHE: roots.http_cache_roots,
        NuGetLocalKind.TEMP: roots.temp_roots,
        NuGetLocalKind.PLUGINS_CACHE: roots.plugins_cache_roots,
    }[kind]


def _override_for_kind(kind: NuGetLocalKind) -> str:
    return {
        NuGetLocalKind.GLOBAL_PACKAGES: "NUGET_PACKAGES",
        NuGetLocalKind.HTTP_CACHE: "NUGET_HTTP_CACHE_PATH",
        NuGetLocalKind.TEMP: "NUGET_SCRATCH",
        NuGetLocalKind.PLUGINS_CACHE: "NUGET_PLUGINS_CACHE_PATH",
    }[kind]


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
    "NuGetClearResult",
    "NuGetLocalEntry",
    "NuGetLocalKind",
    "NuGetMaintenanceLane",
    "NuGetStorageInventory",
    "clear_nuget_local",
    "inventory_nuget_storage",
    "nuget_maintenance_lane",
]
