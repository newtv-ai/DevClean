"""Read-only Go cache inventory plus vendor-supported cleanup operations."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.go_cleanup import (
    clear_go_process_cache,
    go_executable,
    go_process_running,
    go_roots,
)


class GoCacheKind(StrEnum):
    BUILD = "build"
    MODULE = "module"


@dataclass(frozen=True, slots=True)
class GoCacheEntry:
    kind: GoCacheKind
    path: Path
    logical_bytes: int
    exists: bool


@dataclass(frozen=True, slots=True)
class GoStorageInventory:
    caches: tuple[GoCacheEntry, ...]

    @property
    def total_cache_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.caches)


@dataclass(frozen=True, slots=True)
class GoCacheCleanResult:
    kind: GoCacheKind
    path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_go_storage(
    environment: Mapping[str, str] | None = None,
) -> GoStorageInventory:
    """Inventory exact effective Go cache roots without mutation."""

    roots = go_roots(environment)
    pairs = (
        (GoCacheKind.BUILD, roots.build_cache_roots),
        (GoCacheKind.MODULE, roots.module_cache_roots),
    )
    entries: list[GoCacheEntry] = []
    seen: set[tuple[GoCacheKind, str]] = set()
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
                GoCacheEntry(
                    kind=kind,
                    path=path,
                    logical_bytes=_directory_bytes(path) if exists else 0,
                    exists=exists,
                )
            )
    return GoStorageInventory(tuple(entries))


def clean_go_cache(
    kind: GoCacheKind,
    path: Path,
    environment: Mapping[str, str] | None = None,
) -> GoCacheCleanResult:
    """Delegate one exact audited cache clean to the Go command."""

    clear_go_process_cache()
    expected = _roots_for_kind(kind, environment)
    target = _impl._normalize(path)
    if not any(target == _impl._normalize(root) for root in expected):
        raise ValueError(f"不是已审计的 Go {kind.value} cache 路径: {path}")
    if not path.is_dir():
        raise FileNotFoundError(f"Go {kind.value} cache 不存在: {path}")
    if go_process_running():
        raise RuntimeError("Go/gopls 进程正在运行; 请等待完成后再清理")

    before = _directory_bytes(path)
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env[_override_for_kind(kind)] = str(path)
    command = [go_executable(environment), "clean", _flag_for_kind(kind)]
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
        raise RuntimeError(f"无法执行 go clean: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"go clean {_flag_for_kind(kind)} 失败 "
            f"(退出码 {result.returncode}): {detail}"
        )

    after = _directory_bytes(path)
    return GoCacheCleanResult(
        kind=kind,
        path=path,
        before_bytes=before,
        after_bytes=after,
        stdout=result.stdout.strip(),
    )


def _roots_for_kind(
    kind: GoCacheKind,
    environment: Mapping[str, str] | None,
) -> tuple[PureWindowsPath, ...]:
    roots = go_roots(environment)
    return {
        GoCacheKind.BUILD: roots.build_cache_roots,
        GoCacheKind.MODULE: roots.module_cache_roots,
    }[kind]


def _override_for_kind(kind: GoCacheKind) -> str:
    return {
        GoCacheKind.BUILD: "GOCACHE",
        GoCacheKind.MODULE: "GOMODCACHE",
    }[kind]


def _flag_for_kind(kind: GoCacheKind) -> str:
    return {
        GoCacheKind.BUILD: "-cache",
        GoCacheKind.MODULE: "-modcache",
    }[kind]


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
    "GoCacheCleanResult",
    "GoCacheEntry",
    "GoCacheKind",
    "GoStorageInventory",
    "clean_go_cache",
    "inventory_go_storage",
]
