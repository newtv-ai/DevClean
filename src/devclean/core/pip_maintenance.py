"""Read-only pip cache inventory plus vendor-supported purge operations."""

from __future__ import annotations

import os
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

_MIB = 1024**2
_RECOMMEND_BYTES = 512 * _MIB


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
                recommended=size >= _RECOMMEND_BYTES,
                custom=key not in managed_keys,
            )
        )
    return PipStorageInventory(tuple(entries))


def purge_pip_cache(
    cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> PipCachePurgeResult:
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
    if pip_process_running():
        raise RuntimeError("pip 正在运行; 请等待安装/下载操作完成后再清理缓存")

    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["PIP_CACHE_DIR"] = str(cache_path)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    prefix = _validated_pip_command(cache_path, env)
    before = _directory_bytes(cache_path)
    command = (*prefix, "cache", "purge")
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 pip cache purge: {error}") from error
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"pip cache purge 失败 (退出码 {result.returncode}): {output or 'no output'}"
        )
    after = _directory_bytes(cache_path)
    return PipCachePurgeResult(
        cache_path=cache_path,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _validated_pip_command(cache_path: Path, env: dict[str, str]) -> tuple[str, ...]:
    target = _impl._normalize(cache_path)
    for prefix in pip_command_candidates():
        try:
            result = subprocess.run(
                [*prefix, "cache", "dir"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            continue
        candidate = PureWindowsPath(lines[-1].strip().strip('"').strip("'"))
        if candidate.is_absolute() and _impl._normalize(candidate) == target:
            return prefix
    raise RuntimeError("找不到能够确认目标 cache 路径的 pip 命令; 已安全停止")


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
    "PipStorageInventory",
    "inventory_pip_storage",
    "purge_pip_cache",
]
