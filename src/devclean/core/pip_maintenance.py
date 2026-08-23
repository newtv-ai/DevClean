"""Read-only pip cache inventory plus source-bounded vendor purge operations.

pip owns the cache mutation semantics, but the whole configured cache-root size is
not a pre-clean reclaim promise: current ``pip cache purge`` removes pip's cache
items, not arbitrary co-located files in a custom root. Inventory therefore keeps
root occupancy as reporting metadata only and does not derive a recommendation
from that number.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.pip_cleanup import (
    clear_pip_process_cache,
    pip_command_candidates,
    pip_process_running,
    pip_roots,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path


@dataclass(frozen=True, slots=True)
class PipPathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    is_directory: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class PipCommandIdentity:
    prefix: tuple[str, ...]
    executable: PipPathIdentity
    version: str


@dataclass(frozen=True, slots=True)
class PipCacheEntry:
    path: Path
    logical_bytes: int
    exists: bool
    recommended: bool
    custom: bool


@dataclass(frozen=True, slots=True)
class PipStorageInventory:
    caches: tuple[PipCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        """Current provider-root occupancy, not promised purge reclaim."""

        return sum(entry.logical_bytes for entry in self.caches)

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches if entry.recommended)


@dataclass(frozen=True, slots=True)
class PipCachePurgeResult:
    cache_path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_pip_storage(
    environment: Mapping[str, str] | None = None,
) -> PipStorageInventory:
    """Inventory exact pip cache roots without pretending occupancy is reclaim."""

    roots = pip_roots(environment)
    managed_keys = {_impl._normalize(root) for root in roots.managed_cache_roots}
    entries: list[PipCacheEntry] = []
    seen: set[str] = set()
    for raw in (*roots.managed_cache_roots, *roots.custom_cache_roots):
        key = _impl._normalize(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        path = Path(str(raw))
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        size = _directory_bytes(path) if exists else 0
        entries.append(
            PipCacheEntry(
                path=path,
                logical_bytes=size,
                exists=exists,
                recommended=False,
                custom=key not in managed_keys,
            )
        )
    return PipStorageInventory(tuple(entries))


def purge_pip_cache(
    cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> PipCachePurgeResult:
    """Purge one exact audited pip cache through one identity-bound pip command."""

    clear_pip_process_cache()
    roots = pip_roots(environment)
    audited = {
        _impl._normalize(root)
        for root in (*roots.managed_cache_roots, *roots.custom_cache_roots)
    }
    target = _impl._normalize(cache_path)
    if target not in audited:
        raise ValueError(f"不是已审计的 pip cache 根目录: {cache_path}")
    if not cache_path.is_dir():
        raise FileNotFoundError(f"pip cache 不存在: {cache_path}")

    _require_process_idle()
    reviewed_cache = _path_identity(
        cache_path,
        expect_directory=True,
        label="pip cache",
    )

    env = _merged_environment(environment)
    env["PIP_CACHE_DIR"] = str(reviewed_cache.path)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    command_identity = _validated_pip_command(reviewed_cache.path, env)

    # Revalidate every mutable boundary immediately before mutation. For
    # ``python/py -m pip``, the executable identity binds the launcher/interpreter
    # while the exact ``pip --version`` text binds the selected pip environment.
    _require_process_idle()
    _require_same_path_identity(command_identity.executable, "pip 命令")
    _require_same_path_identity(reviewed_cache, "pip cache")
    _confirm_pip_command(command_identity, reviewed_cache.path, env)

    before = _directory_bytes(reviewed_cache.path)
    command = (*command_identity.prefix, "cache", "purge")
    result = _run_pip(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"pip cache purge 失败 (退出码 {result.returncode}): {output or 'no output'}"
        )

    _require_same_path_identity(command_identity.executable, "pip 命令")
    _require_same_path_identity(reviewed_cache, "pip cache")
    after = _directory_bytes(reviewed_cache.path)
    return PipCachePurgeResult(
        cache_path=reviewed_cache.path,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _validated_pip_command(
    cache_path: Path,
    env: dict[str, str],
) -> PipCommandIdentity:
    target = _impl._normalize(cache_path)
    for raw_prefix in pip_command_candidates():
        try:
            prefix = _resolve_command_prefix(raw_prefix, env)
            executable = _path_identity(
                Path(prefix[0]),
                expect_directory=False,
                label="pip 命令",
            )
            version = _pip_version(prefix, env)
            reported_cache = _pip_cache_dir(prefix, env)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if reported_cache is None or _impl._normalize(reported_cache) != target:
            continue
        return PipCommandIdentity(prefix, executable, version)
    raise RuntimeError("找不到能够确认目标 cache 路径的 pip 命令; 已安全停止")


def _confirm_pip_command(
    identity: PipCommandIdentity,
    cache_path: Path,
    env: dict[str, str],
) -> None:
    if _pip_version(identity.prefix, env) != identity.version:
        raise RuntimeError("pip 环境身份在执行前发生变化; 请重新扫描")
    reported_cache = _pip_cache_dir(identity.prefix, env)
    if (
        reported_cache is None
        or _impl._normalize(reported_cache) != _impl._normalize(cache_path)
    ):
        raise RuntimeError("pip 未再次确认目标 cache 路径; 已安全停止")


def _pip_version(prefix: tuple[str, ...], env: dict[str, str]) -> str:
    result = _run_pip((*prefix, "--version"), env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("pip --version 失败")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("pip --version 返回结果不唯一")
    return lines[0]


def _pip_cache_dir(prefix: tuple[str, ...], env: dict[str, str]) -> Path | None:
    result = _run_pip((*prefix, "cache", "dir"), env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("pip cache dir 失败")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("pip cache dir 返回结果不唯一")
    raw = lines[0].strip().strip('"').strip("'")
    candidate = PureWindowsPath(raw)
    if candidate.is_absolute():
        return Path(str(candidate))
    native = Path(raw).expanduser()
    return Path(os.path.abspath(native)) if native.is_absolute() else None


def _resolve_command_prefix(
    raw_prefix: tuple[str, ...],
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    if not raw_prefix:
        raise RuntimeError("空 pip 命令")
    raw_executable = raw_prefix[0]
    candidate = Path(raw_executable).expanduser()
    if not candidate.is_absolute():
        resolved = shutil.which(
            raw_executable,
            path=_environment_value(environment, "PATH"),
        )
        if resolved is None:
            raise FileNotFoundError(f"未找到 pip 命令: {raw_executable}")
        candidate = Path(resolved)
    identity = _path_identity(candidate, expect_directory=False, label="pip 命令")
    return (str(identity.path), *raw_prefix[1:])


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> PipPathIdentity:
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
    return PipPathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        is_directory=metadata.is_directory,
        creation_time_ns=None if expect_directory else metadata.creation_time_ns,
        last_write_time_ns=None if expect_directory else metadata.last_write_time_ns,
    )


def _require_same_path_identity(reviewed: PipPathIdentity, label: str) -> None:
    current = _path_identity(
        reviewed.path,
        expect_directory=reviewed.is_directory,
        label=label,
    )
    if current != reviewed:
        raise RuntimeError(f"{label} 身份发生变化; 请重新扫描")


def _require_process_idle() -> None:
    clear_pip_process_cache()
    if pip_process_running():
        raise RuntimeError("pip 正在运行; 请等待安装/下载操作完成后再清理缓存")


def _run_pip(
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
        raise RuntimeError(f"无法执行 pip: {error}") from error


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
    "PipCacheEntry",
    "PipCachePurgeResult",
    "PipCommandIdentity",
    "PipPathIdentity",
    "PipStorageInventory",
    "inventory_pip_storage",
    "purge_pip_cache",
]
