"""Read-only pnpm inventory plus vendor-supported store pruning."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.pnpm_cleanup import pnpm_process_running, pnpm_roots


@dataclass(frozen=True, slots=True)
class PnpmStoreEntry:
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class PnpmStorageInventory:
    stores: tuple[PnpmStoreEntry, ...]

    @property
    def total_store_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.stores)


@dataclass(frozen=True, slots=True)
class PnpmPruneResult:
    store_path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

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
        key = os.path.normcase(os.path.normpath(str(path)))
        if key in seen:
            continue
        seen.add(key)
        try:
            exists = path.is_dir()
        except OSError:
            exists = False
        entries.append(
            PnpmStoreEntry(
                path=path,
                logical_bytes=_directory_bytes(path) if exists else 0,
                exists=exists,
            )
        )
    return PnpmStorageInventory(tuple(entries))


def prune_pnpm_store(store_path: Path) -> PnpmPruneResult:
    """Run pnpm's own prune algorithm for one selected store root."""

    root = _store_config_root(store_path)
    if pnpm_process_running():
        raise RuntimeError("pnpm 正在运行，请关闭正在执行的 pnpm 命令后再清理 store")
    if not root.is_dir():
        raise FileNotFoundError(f"pnpm store 不存在：{root}")

    before = _directory_bytes(root)
    executable = "pnpm.cmd" if os.name == "nt" else "pnpm"
    env = dict(os.environ)
    env["PNPM_CONFIG_STORE_DIR"] = str(root)
    try:
        result = subprocess.run(
            [executable, "store", "prune"],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 pnpm store prune：{error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"pnpm store prune 失败（退出码 {result.returncode}）：{detail}"
        )
    after = _directory_bytes(root)
    return PnpmPruneResult(
        store_path=root,
        before_bytes=before,
        after_bytes=after,
        stdout=result.stdout.strip(),
    )


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


__all__ = [
    "PnpmPruneResult",
    "PnpmStorageInventory",
    "PnpmStoreEntry",
    "inventory_pnpm_storage",
    "prune_pnpm_store",
]
