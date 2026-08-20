"""Read-only Conda cache inventory plus vendor-supported cache cleanup.

Conda's package-cache root mixes safely disposable download/index cache with
extracted package trees that may be link sources for environments. DevClean
therefore never raw-deletes the package cache and never requests ``--packages``
or ``--all``. It scopes one exact audited cache, asks Conda to confirm that root,
and delegates only tarball/index-cache cleanup to ``conda clean``.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.conda_cleanup import (
    clear_conda_process_cache,
    conda_executable,
    conda_process_running,
    conda_roots,
)

_GIB = 1024**3
_RECOMMEND_BYTES = _GIB


@dataclass(frozen=True, slots=True)
class CondaPackageCacheEntry:
    path: Path
    logical_bytes: int
    exists: bool
    recommended: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CondaStorageInventory:
    package_caches: tuple[CondaPackageCacheEntry, ...]

    @property
    def total_package_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.package_caches)

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.package_caches if entry.recommended)


@dataclass(frozen=True, slots=True)
class CondaCleanResult:
    package_cache_path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

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
            CondaPackageCacheEntry(
                path=path,
                logical_bytes=size,
                exists=exists,
                recommended=size >= _RECOMMEND_BYTES,
                reason=(
                    "Conda 官方 tarball/index cache 可安全清理; extracted packages 保留不动"
                ),
            )
        )
    return CondaStorageInventory(tuple(entries))


def clean_conda_package_cache(
    package_cache_path: Path,
    environment: Mapping[str, str] | None = None,
) -> CondaCleanResult:
    """Clear only Conda tarballs/index data for one exact audited cache root."""

    clear_conda_process_cache()
    roots = conda_roots(environment).package_cache_roots
    target = _impl._normalize(package_cache_path)
    if not target or not any(target == _impl._normalize(root) for root in roots):
        raise ValueError(f"不是已审计的 Conda package cache: {package_cache_path}")

    root = package_cache_path
    if not root.is_dir():
        raise FileNotFoundError(f"Conda package cache 不存在: {root}")
    if conda_process_running():
        raise RuntimeError("Conda/Mamba 正在运行; 请等待包管理操作完成后再清理缓存")

    executable = conda_executable(environment)
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env["CONDA_PKGS_DIRS"] = str(root)

    info_command = (executable, "info", "--json")
    info_result = _run_conda(info_command, env, timeout=60)
    if info_result.returncode != 0:
        detail = _combined_output(info_result.stdout, info_result.stderr)
        raise RuntimeError(
            f"conda info --json 失败 (退出码 {info_result.returncode}): "
            f"{detail or 'no output'}"
        )
    confirmed = _confirmed_package_caches(info_result.stdout)
    if target not in {_impl._normalize(path) for path in confirmed}:
        raise RuntimeError("Conda 未确认所选 package cache; 已安全停止")

    before = _directory_bytes(root)
    command = (
        executable,
        "clean",
        "--tarballs",
        "--index-cache",
        "--yes",
        "--json",
    )
    result = _run_conda(command, env, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"conda clean 失败 (退出码 {result.returncode}): {output or 'no output'}"
        )

    after = _directory_bytes(root)
    return CondaCleanResult(
        package_cache_path=root,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _run_conda(
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
        raise RuntimeError(f"无法执行 Conda: {error}") from error


def _confirmed_package_caches(stdout: str | None) -> tuple[Path, ...]:
    try:
        payload = json.loads(stdout or "")
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    value = payload.get("pkgs_dirs")
    if not isinstance(value, list):
        return ()
    found: list[Path] = []
    for item in value:
        if not isinstance(item, str):
            continue
        candidate = PureWindowsPath(item)
        if candidate.is_absolute():
            found.append(Path(str(candidate)))
    return tuple(found)


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
    "CondaCleanResult",
    "CondaPackageCacheEntry",
    "CondaStorageInventory",
    "clean_conda_package_cache",
    "inventory_conda_storage",
]
