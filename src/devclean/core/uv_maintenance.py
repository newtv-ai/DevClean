"""Read-only uv inventory plus vendor-supported cache pruning."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.uv_cleanup import uv_process_running, uv_roots


@dataclass(frozen=True, slots=True)
class UvCacheEntry:
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class UvStorageInventory:
    caches: tuple[UvCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches)


@dataclass(frozen=True, slots=True)
class UvPruneResult:
    cache_path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

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
        key = _normalized(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        entries.append(
            UvCacheEntry(
                path=path,
                logical_bytes=_directory_bytes(path) if exists else 0,
                exists=exists,
            )
        )
    return UvStorageInventory(tuple(entries))


def prune_uv_cache(
    cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> UvPruneResult:
    """Run uv's own periodic prune operation for an exact audited cache root."""

    root = cache_path
    audited = {_normalized(Path(str(path))) for path in uv_roots(environment).cache_roots}
    if _normalized(root) not in audited:
        raise ValueError(f"不是已审计的 uv cache 根目录: {root}")
    if uv_process_running():
        raise RuntimeError("uv 正在运行; 请关闭正在执行的 uv/uvx 命令后再清理缓存")
    if not root.is_dir():
        raise FileNotFoundError(f"uv cache 不存在: {root}")

    before = _directory_bytes(root)
    executable = "uv.exe" if os.name == "nt" else "uv"
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["UV_CACHE_DIR"] = str(root)
    try:
        result = subprocess.run(
            [executable, "cache", "prune"],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 uv cache prune: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"uv cache prune 失败 (退出码 {result.returncode}): {detail}"
        )
    after = _directory_bytes(root)
    return UvPruneResult(
        cache_path=root,
        before_bytes=before,
        after_bytes=after,
        stdout=result.stdout.strip(),
    )


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


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
    "UvCacheEntry",
    "UvPruneResult",
    "UvStorageInventory",
    "inventory_uv_storage",
    "prune_uv_cache",
]
