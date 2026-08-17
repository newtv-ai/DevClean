"""Read-only Hugging Face cache inventory and vendor-supported Hub pruning."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.huggingface_cleanup import (
    clear_huggingface_process_cache,
    hf_executable,
    huggingface_process_running,
    huggingface_roots,
)


class HuggingFaceCacheKind(StrEnum):
    HUB = "hub"
    XET = "xet"
    ASSETS = "assets"


@dataclass(frozen=True, slots=True)
class HuggingFaceCacheEntry:
    kind: HuggingFaceCacheKind
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class HuggingFaceStorageInventory:
    caches: tuple[HuggingFaceCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.caches)


@dataclass(frozen=True, slots=True)
class HuggingFacePruneResult:
    path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_huggingface_storage(
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceStorageInventory:
    roots = huggingface_roots(environment)
    pairs = (
        (HuggingFaceCacheKind.HUB, roots.hub_cache_roots),
        (HuggingFaceCacheKind.XET, roots.xet_cache_roots),
        (HuggingFaceCacheKind.ASSETS, roots.assets_cache_roots),
    )
    entries: list[HuggingFaceCacheEntry] = []
    seen: set[tuple[HuggingFaceCacheKind, str]] = set()
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
            entries.append(
                HuggingFaceCacheEntry(
                    kind=kind,
                    path=path,
                    logical_bytes=_directory_bytes(path) if exists else 0,
                    exists=exists,
                )
            )
    return HuggingFaceStorageInventory(tuple(entries))


def prune_huggingface_hub_cache(
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> HuggingFacePruneResult:
    """Run ``hf cache prune`` only for an exact audited Hub cache root."""

    clear_huggingface_process_cache()
    expected = huggingface_roots(environment).hub_cache_roots
    target = _impl._normalize(path)
    if not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 Hugging Face Hub cache 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"Hugging Face Hub cache 不存在: {path}")
    if huggingface_process_running():
        raise RuntimeError("Hugging Face/Transformers 相关进程正在运行; 请稍后再清理")

    before = _directory_bytes(path)
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["HF_HUB_CACHE"] = str(path)
    command = [
        hf_executable(environment),
        "cache",
        "prune",
        "--cache-dir",
        str(path),
        "--yes",
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
        raise RuntimeError(f"无法执行 hf cache prune: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"hf cache prune 失败 (退出码 {result.returncode}): {detail}"
        )

    after = _directory_bytes(path)
    return HuggingFacePruneResult(
        path=path,
        before_bytes=before,
        after_bytes=after,
        stdout=result.stdout.strip(),
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
    "HuggingFaceCacheEntry",
    "HuggingFaceCacheKind",
    "HuggingFacePruneResult",
    "HuggingFaceStorageInventory",
    "inventory_huggingface_storage",
    "prune_huggingface_hub_cache",
]
