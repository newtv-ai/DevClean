"""Read-only Conda cache inventory plus vendor-supported cache cleanup."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.conda_cleanup import (
    clear_conda_process_cache,
    conda_executable,
    conda_process_running,
    conda_roots,
)


@dataclass(frozen=True, slots=True)
class CondaPackageCacheEntry:
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class CondaStorageInventory:
    package_caches: tuple[CondaPackageCacheEntry, ...]

    @property
    def total_package_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.package_caches)


@dataclass(frozen=True, slots=True)
class CondaCleanResult:
    package_cache_path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_conda_storage(
    environment: Mapping[str, str] | None = None,
) -> CondaStorageInventory:
    """Inventory exact effective package-cache roots without modifying them."""

    entries: list[CondaPackageCacheEntry] = []
    seen: set[str] = set()
    for raw in conda_roots(environment).package_cache_roots:
        path = Path(str(raw))
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        entries.append(
            CondaPackageCacheEntry(
                path=path,
                logical_bytes=_directory_bytes(path) if exists else 0,
                exists=exists,
            )
        )
    return CondaStorageInventory(tuple(entries))


def clean_conda_package_cache(
    package_cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> CondaCleanResult:
    """Use Conda to clear cached archives/index data for one audited cache root.

    DevClean deliberately does not request ``--packages``, ``--all`` or
    ``--force-pkgs-dirs``. Conda warns that package-cache removal can break
    environments that rely on symlinks back to extracted packages. The selected
    exact cache is instead scoped through ``CONDA_PKGS_DIRS`` and Conda removes
    only cached tarballs and index data through its own implementation.
    """

    clear_conda_process_cache()
    roots = conda_roots(environment).package_cache_roots
    target = _impl._normalize(package_cache_path)
    if not any(target == _impl._normalize(root) for root in roots):
        raise ValueError(f"不是已审计的 Conda package cache: {package_cache_path}")

    root = package_cache_path
    if not root.is_dir():
        raise FileNotFoundError(f"Conda package cache 不存在: {root}")
    if conda_process_running():
        raise RuntimeError("Conda/Mamba 正在运行; 请等待包管理操作完成后再清理缓存")

    before = _directory_bytes(root)
    executable = conda_executable(environment)
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["CONDA_PKGS_DIRS"] = str(root)
    try:
        result = subprocess.run(
            [
                executable,
                "clean",
                "--tarballs",
                "--index-cache",
                "--yes",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 conda clean: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"conda clean 失败 (退出码 {result.returncode}): {detail}"
        )

    after = _directory_bytes(root)
    return CondaCleanResult(
        package_cache_path=root,
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
    "CondaCleanResult",
    "CondaPackageCacheEntry",
    "CondaStorageInventory",
    "clean_conda_package_cache",
    "inventory_conda_storage",
]
