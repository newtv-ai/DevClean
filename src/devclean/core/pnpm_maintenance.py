"""Read-only pnpm inventory plus vendor-supported store garbage collection.

``pnpm store prune`` is a particularly strong deterministic cleanup primitive:
pnpm itself decides which packages are unreferenced by every registered project.
DevClean therefore does not inspect or delete store internals. It validates the
selected store against current pnpm discovery, asks pnpm to confirm that same
store, and then delegates garbage collection to the vendor command.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.pnpm_cleanup import (
    clear_pnpm_process_cache,
    pnpm_process_running,
    pnpm_roots,
)

_GIB = 1024**3
_RECOMMEND_BYTES = _GIB


@dataclass(frozen=True, slots=True)
class PnpmStoreEntry:
    path: Path
    logical_bytes: int
    exists: bool
    recommended: bool


@dataclass(frozen=True, slots=True)
class PnpmStorageInventory:
    stores: tuple[PnpmStoreEntry, ...]

    @property
    def total_store_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores)

    @property
    def recommended_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores if entry.recommended)


@dataclass(frozen=True, slots=True)
class PnpmPruneResult:
    store_path: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_pnpm_storage(
    environment: Mapping[str, str] | None = None,
) -> PnpmStorageInventory:
    """Inventory every discovered pnpm store without modifying it."""

    entries: list[PnpmStoreEntry] = []
    seen: set[str] = set()
    for raw in pnpm_roots(environment).store_roots:
        path = _store_config_root(Path(str(raw)))
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
            PnpmStoreEntry(
                path=path,
                logical_bytes=size,
                exists=exists,
                recommended=size >= _RECOMMEND_BYTES,
            )
        )
    return PnpmStorageInventory(tuple(entries))


def prune_pnpm_store(
    store_path: Path,
    environment: Mapping[str, str] | None = None,
) -> PnpmPruneResult:
    """Run pnpm's own GC for one exact, currently audited store root."""

    clear_pnpm_process_cache()
    root = _store_config_root(store_path)
    target = _impl._normalize(root)
    audited = {
        _impl._normalize(_store_config_root(Path(str(raw))))
        for raw in pnpm_roots(environment).store_roots
    }
    if not target or target not in audited:
        raise ValueError(f"不是当前已审计的 pnpm store 根目录: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"pnpm store 不存在: {root}")
    if pnpm_process_running():
        raise RuntimeError("pnpm 正在运行; 请等待当前 pnpm 操作完成后再清理 store")

    executable = _pnpm_executable(environment)
    scope = (executable, "--store-dir", str(root))
    active_command = (*scope, "store", "path", "--silent")
    active = _run_pnpm(active_command, environment, timeout=60)
    if active.returncode != 0:
        detail = _combined_output(active.stdout, active.stderr)
        raise RuntimeError(
            f"pnpm store path 失败 (退出码 {active.returncode}): {detail or 'no output'}"
        )
    active_root = _parse_store_path(active.stdout)
    if active_root is None or _impl._normalize(active_root) != target:
        raise RuntimeError("pnpm 未确认所选 store 路径; 已安全停止")

    before = _directory_bytes(root)
    command = (*scope, "store", "prune")
    result = _run_pnpm(command, environment, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"pnpm store prune 失败 (退出码 {result.returncode}): {output or 'no output'}"
        )
    after = _directory_bytes(root)
    return PnpmPruneResult(
        store_path=root,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def _pnpm_executable(environment: Mapping[str, str] | None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_pnpm_exe")
    if configured:
        return configured
    return "pnpm.cmd" if os.name == "nt" else "pnpm"


def _run_pnpm(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 pnpm: {error}") from error


def _parse_store_path(stdout: str | None) -> Path | None:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    candidate = PureWindowsPath(lines[-1].strip().strip('"').strip("'"))
    if not candidate.is_absolute():
        return None
    return _store_config_root(Path(str(candidate)))


def _store_config_root(path: Path) -> Path:
    """Convert a versioned store path like store/v10 back to store-dir."""

    name = path.name.casefold()
    if len(name) > 1 and name.startswith("v") and name[1:].isdigit():
        return path.parent
    return path


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


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "PnpmPruneResult",
    "PnpmStorageInventory",
    "PnpmStoreEntry",
    "inventory_pnpm_storage",
    "prune_pnpm_store",
]
