"""Read-only uv inventory plus vendor-supported cache pruning.

Astral explicitly documents direct cache mutation as unsafe and ``uv cache
prune`` as a periodic safe cleanup operation for unused entries. DevClean
therefore validates an exact effective cache root, asks the selected uv binary
to confirm that same root, and delegates all mutation to uv itself.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.uv_cleanup import clear_uv_process_cache, uv_process_running, uv_roots

_MIB = 1024**2
_RECOMMEND_BYTES = 512 * _MIB


@dataclass(frozen=True, slots=True)
class UvCacheEntry:
    path: Path
    logical_bytes: int
    exists: bool
    recommended: bool


@dataclass(frozen=True, slots=True)
class UvStorageInventory:
    caches: tuple[UvCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches)

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches if entry.recommended)


@dataclass(frozen=True, slots=True)
class UvPruneResult:
    cache_path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_uv_storage(
    environment: Mapping[str, str] | None = None,
) -> UvStorageInventory:
    """Inventory discovered uv cache roots without modifying them."""

    entries: list[UvCacheEntry] = []
    seen: set[str] = set()
    for raw in uv_roots(environment).cache_roots:
        path = Path(str(raw))
        key = _impl._normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        size = _directory_bytes(path) if exists else 0
        entries.append(
            UvCacheEntry(
                path=path,
                logical_bytes=size,
                exists=exists,
                recommended=size >= _RECOMMEND_BYTES,
            )
        )
    return UvStorageInventory(tuple(entries))


def prune_uv_cache(
    cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> UvPruneResult:
    """Run uv's own periodic prune operation for an exact audited cache root."""

    clear_uv_process_cache()
    root = cache_path
    audited = {
        _impl._normalize(Path(str(path))) for path in uv_roots(environment).cache_roots
    }
    target = _impl._normalize(root)
    if not target or target not in audited:
        raise ValueError(f"不是已审计的 uv cache 根目录: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"uv cache 不存在: {root}")
    if uv_process_running():
        raise RuntimeError("uv 正在运行; 请等待当前 uv/uvx 命令完成后再清理缓存")

    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["UV_CACHE_DIR"] = str(root)
    executable = _uv_executable(environment)

    confirmed = _run_uv((executable, "cache", "dir"), env, timeout=60)
    if confirmed.returncode != 0:
        detail = _combined_output(confirmed.stdout, confirmed.stderr)
        raise RuntimeError(
            f"uv cache dir 失败 (退出码 {confirmed.returncode}): {detail or 'no output'}"
        )
    confirmed_path = _parse_cache_dir(confirmed.stdout)
    if confirmed_path is None or _impl._normalize(confirmed_path) != target:
        raise RuntimeError("uv 未确认所选 cache 路径; 已安全停止")

    before = _directory_bytes(root)
    command = (executable, "cache", "prune")
    result = _run_uv(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"uv cache prune 失败 (退出码 {result.returncode}): {output or 'no output'}"
        )
    after = _directory_bytes(root)
    return UvPruneResult(
        cache_path=root,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _uv_executable(environment: Mapping[str, str] | None) -> str:
    source = os.environ if environment is None else environment
    env = {key.casefold(): value for key, value in source.items() if value}
    configured = env.get("devclean_uv_exe")
    if configured:
        return configured
    return "uv.exe" if os.name == "nt" else "uv"


def _run_uv(
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
        raise RuntimeError(f"无法执行 uv: {error}") from error


def _parse_cache_dir(stdout: str | None) -> Path | None:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    candidate = PureWindowsPath(lines[-1].strip().strip('"').strip("'"))
    return Path(str(candidate)) if candidate.is_absolute() else None


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
    "UvCacheEntry",
    "UvPruneResult",
    "UvStorageInventory",
    "inventory_uv_storage",
    "prune_uv_cache",
]
