"""Read-only NuGet local inventory plus identity-bound vendor clear operations.

NuGet's individual ``locals <kind> --clear`` commands own the mutation semantics
for their exact local-resource roots. DevClean keeps the existing lane split:
HTTP, temporary, and plugin caches are deterministic vendor-maintained storage,
while ``global-packages`` remains user-review dependency storage.
"""

from __future__ import annotations

import os
import shutil
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
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

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
class NuGetPathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    is_directory: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None


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
    command: tuple[str, ...]
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
    """Clear one exact audited local resource through one identity-bound .NET CLI."""

    clear_nuget_process_cache()
    expected = _roots_for_kind(kind, environment)
    target = _impl._normalize(path)
    if not target or not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 NuGet {kind.value} 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"NuGet {kind.value} 不存在: {path}")

    _require_process_idle()
    reviewed_root = _path_identity(
        path,
        expect_directory=True,
        label=f"NuGet {kind.value}",
    )

    env = _merged_environment(environment)
    env[_override_for_kind(kind)] = str(reviewed_root.path)
    reviewed_dotnet = _resolved_dotnet_identity(environment, env)
    _confirm_vendor_root(kind, reviewed_dotnet.path, reviewed_root.path, env)

    # Revalidate every mutable boundary immediately before the vendor mutation.
    _require_process_idle()
    _require_same_path_identity(reviewed_dotnet, ".NET CLI")
    _require_same_path_identity(reviewed_root, f"NuGet {kind.value}")
    _confirm_vendor_root(kind, reviewed_dotnet.path, reviewed_root.path, env)

    before = _directory_bytes(reviewed_root.path)
    command = (
        str(reviewed_dotnet.path),
        "nuget",
        "locals",
        kind.value,
        "--clear",
        "--force-english-output",
    )
    result = _run_dotnet(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"dotnet nuget locals {kind.value} --clear 失败 "
            f"(退出码 {result.returncode}): {output or 'no output'}"
        )

    _require_same_path_identity(reviewed_dotnet, ".NET CLI")
    # Current NuGet LocalResourceUtils.DeleteDirectoryTree removes the selected
    # root itself. A successful exit with the root still present is therefore not
    # a proved complete clear; never finish it with a raw filesystem fallback.
    if os.path.lexists(reviewed_root.path):
        raise RuntimeError(
            f"NuGet {kind.value} 官方清理成功返回但目标根目录仍存在; 已停止且不会直接删除"
        )

    return NuGetClearResult(
        kind=kind,
        path=reviewed_root.path,
        before_bytes=before,
        after_bytes=0,
        command=command,
        stdout=result.stdout.strip(),
    )


def _confirm_vendor_root(
    kind: NuGetLocalKind,
    executable: Path,
    expected: Path,
    environment: dict[str, str],
) -> None:
    reported = _listed_local_path(kind, executable, environment)
    if reported is None or _impl._normalize(reported) != _impl._normalize(expected):
        raise RuntimeError(
            f"dotnet nuget locals 未确认所选 {kind.value} 路径; 已安全停止"
        )


def _listed_local_path(
    kind: NuGetLocalKind,
    executable: Path,
    environment: dict[str, str],
) -> Path | None:
    command = (
        str(executable),
        "nuget",
        "locals",
        kind.value,
        "--list",
        "--force-english-output",
    )
    result = _run_dotnet(command, environment, timeout=60)
    if result.returncode != 0:
        detail = _combined_output(result.stdout, result.stderr)
        raise RuntimeError(
            f"dotnet nuget locals {kind.value} --list 失败 "
            f"(退出码 {result.returncode}): {detail or 'no output'}"
        )

    found: list[Path] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().casefold() != kind.value.casefold():
            continue
        candidate_text = value.strip().strip('"').strip("'")
        candidate = PureWindowsPath(candidate_text)
        if candidate.is_absolute():
            found.append(Path(str(candidate)))
    if len(found) != 1:
        return None
    return found[0]


def _resolved_dotnet_identity(
    environment: Mapping[str, str] | None,
    merged_environment: Mapping[str, str],
) -> NuGetPathIdentity:
    raw = dotnet_executable(environment)
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        resolved = shutil.which(
            raw,
            path=_environment_value(merged_environment, "PATH"),
        )
        if resolved is None:
            raise FileNotFoundError(f"未找到 .NET CLI: {raw}")
        candidate = Path(resolved)
    return _path_identity(candidate, expect_directory=False, label=".NET CLI")


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> NuGetPathIdentity:
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
    return NuGetPathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        is_directory=metadata.is_directory,
        creation_time_ns=None if expect_directory else metadata.creation_time_ns,
        last_write_time_ns=None if expect_directory else metadata.last_write_time_ns,
    )


def _require_same_path_identity(reviewed: NuGetPathIdentity, label: str) -> None:
    current = _path_identity(
        reviewed.path,
        expect_directory=reviewed.is_directory,
        label=label,
    )
    if current != reviewed:
        raise RuntimeError(f"{label} 身份发生变化; 请重新扫描")


def _require_process_idle() -> None:
    clear_nuget_process_cache()
    if nuget_process_running():
        raise RuntimeError("NuGet/.NET restore 或构建进程正在运行; 请等待完成后再清理")


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


def _run_dotnet(
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
        raise RuntimeError(f"无法执行 dotnet nuget locals: {error}") from error


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
    "NuGetClearResult",
    "NuGetLocalEntry",
    "NuGetLocalKind",
    "NuGetMaintenanceLane",
    "NuGetPathIdentity",
    "NuGetStorageInventory",
    "clear_nuget_local",
    "inventory_nuget_storage",
    "nuget_maintenance_lane",
]
