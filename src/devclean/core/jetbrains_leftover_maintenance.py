"""Source-backed JetBrains expired system-directory maintenance for Windows."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devclean.core.jetbrains_cleanup import (
    clear_jetbrains_process_cache,
    jetbrains_process_running,
)
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_exact_directory_tree,
)
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_WINDOWS = os.name == "nt"
_DAY_NS = 86_400 * 1_000_000_000
_VENDOR_SHELF_LIFE_DAYS = 180
_SELECTOR_PREFIXES = (
    "IntelliJIdea",
    "IdeaIC",
    "PyCharm",
    "PyCharmCE",
    "WebStorm",
    "PhpStorm",
    "CLion",
    "DataGrip",
    "GoLand",
    "Rider",
    "RubyMine",
    "RustRover",
    "DataSpell",
    "Aqua",
    "MPS",
)
_SELECTOR_RE = re.compile(
    rf"^(?:{'|'.join(re.escape(item) for item in _SELECTOR_PREFIXES)})"
    r"(?P<version>\d{4}\.\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class JetBrainsTreeStats:
    logical_bytes: int
    entry_count: int
    latest_write_time_ns: int


@dataclass(frozen=True, slots=True)
class JetBrainsLeftoverInventory:
    selector: str
    config_root: Path
    system_root: Path
    config_identity: ExactDirectorySnapshot
    system_identity: ExactDirectorySnapshot
    stats: JetBrainsTreeStats
    stale_days: float
    vendor_expired: bool
    installed: bool | None
    cleanup_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class JetBrainsLeftoverCleanupResult:
    before: JetBrainsLeftoverInventory
    purge: DirectoryPurgeResult

    @property
    def root_absent(self) -> bool:
        return self.purge.completed and self.purge.root_absent


def inventory_jetbrains_expired_system_directories(
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[JetBrainsLeftoverInventory, ...]:
    """Mirror JetBrains' automatic 180-day default system-dir expiry conservatively."""

    if not _WINDOWS:
        raise RuntimeError("JetBrains 过期版本存储维护仅支持 Windows")

    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    if not appdata or not localappdata:
        return ()

    config_parent = Path(appdata) / "JetBrains"
    system_parent = Path(localappdata) / "JetBrains"
    if not config_parent.is_dir() or not system_parent.is_dir():
        return ()

    current_ns = _now_ns(now)
    try:
        children = tuple(config_parent.iterdir())
    except OSError:
        return ()

    inventories: list[JetBrainsLeftoverInventory] = []
    for config_root in children:
        selector = config_root.name
        if not _supported_selector(selector):
            continue
        system_root = system_parent / selector
        if not system_root.is_dir():
            continue
        try:
            inventory = _inspect_selector(
                selector,
                config_root,
                system_root,
                current_ns=current_ns,
            )
        except (OSError, RuntimeError, ValueError):
            continue
        inventories.append(inventory)

    return tuple(sorted(inventories, key=lambda item: item.selector.casefold()))


def cleanup_jetbrains_expired_system_directory(
    expected: JetBrainsLeftoverInventory,
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> JetBrainsLeftoverCleanupResult:
    """Delete one exact vendor-expired, uninstalled default system directory."""

    if not _WINDOWS:
        raise RuntimeError("JetBrains 过期版本存储维护仅支持 Windows")
    if not expected.cleanup_supported or not expected.vendor_expired:
        raise ValueError("用户选择的 JetBrains 目录没有满足厂商 180 天过期边界")
    if expected.installed is not False:
        raise ValueError("仍安装或安装状态不确定的 JetBrains 版本不会由 DevClean 删除")

    _require_no_jetbrains_process()

    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    if not appdata or not localappdata:
        raise RuntimeError("无法重新解析 JetBrains 默认目录")
    config_root = Path(appdata) / "JetBrains" / expected.selector
    system_root = Path(localappdata) / "JetBrains" / expected.selector
    current = _inspect_selector(
        expected.selector,
        config_root,
        system_root,
        current_ns=_now_ns(now),
    )
    if not current.cleanup_supported or current.installed is not False:
        raise RuntimeError("JetBrains 过期目录状态已变化；请重新检查")
    if (
        current.config_root != expected.config_root
        or current.system_root != expected.system_root
        or current.config_identity != expected.config_identity
        or current.system_identity != expected.system_identity
        or current.stats != expected.stats
    ):
        raise RuntimeError("JetBrains 目录身份或内容在确认后发生变化；拒绝删除")

    # Process state is intentionally refreshed again after the expensive tree
    # re-scan so an IDE launched during confirmation cannot race the mutation.
    _require_no_jetbrains_process()

    boundary_path = current.system_root.parent
    if not is_local_fixed_path(boundary_path):
        raise ValueError("JetBrains system 父边界不在本地固定磁盘")
    boundary = _exact_root_boundary(boundary_path)
    result = purge_exact_directory_tree(
        current.system_root,
        current.system_identity,
        boundary,
    )
    if not result.completed or not result.root_absent:
        raise RuntimeError("JetBrains 过期 system 目录精确删除未完整完成")
    return JetBrainsLeftoverCleanupResult(before=current, purge=result)


def _inspect_selector(
    selector: str,
    config_root: Path,
    system_root: Path,
    *,
    current_ns: int,
) -> JetBrainsLeftoverInventory:
    if not _supported_selector(selector):
        raise ValueError(f"不是受审计的现代 JetBrains 产品版本目录: {selector}")

    config = _ordinary_resolved_directory(config_root, "JetBrains 配置目录")
    system = _ordinary_resolved_directory(system_root, "JetBrains system 目录")
    if (
        config.name.casefold() != selector.casefold()
        or system.name.casefold() != selector.casefold()
    ):
        raise ValueError("JetBrains 配置/system 目录版本标识不一致")

    config_identity = _exact_directory_snapshot(config, "JetBrains 配置目录")
    system_before, root_write_before = _directory_probe(system, "JetBrains system 目录")
    stats = _tree_stats(system)
    system_after, root_write_after = _directory_probe(system, "JetBrains system 目录")
    if system_before != system_after or root_write_before != root_write_after:
        raise RuntimeError("JetBrains system 目录在检查期间发生变化")

    installed = _installed_state(system, selector)
    cutoff = current_ns - _VENDOR_SHELF_LIFE_DAYS * _DAY_NS
    vendor_expired = stats.latest_write_time_ns <= cutoff
    stale_days = max(0.0, (current_ns - stats.latest_write_time_ns) / _DAY_NS)

    if installed is None:
        supported = False
        reason = "无法可靠确认旧目录对应的 IDE 安装状态；只报告不删除"
    elif installed:
        supported = False
        reason = "该版本仍对应一个可见的 JetBrains 安装；DevClean 比厂商自动清理更保守并保持它"
    elif not vendor_expired:
        supported = False
        reason = f"最近更新约 {stale_days:.0f} 天，尚未达到 JetBrains 的 180 天自动清理期限"
    else:
        supported = True
        reason = (
            "该默认 system 目录已超过 JetBrains 源码使用的 180 天 shelf life，"
            "且没有对应的现存安装；属于厂商过期存储候选"
        )

    return JetBrainsLeftoverInventory(
        selector=selector,
        config_root=config,
        system_root=system,
        config_identity=config_identity,
        system_identity=system_after,
        stats=stats,
        stale_days=stale_days,
        vendor_expired=vendor_expired,
        installed=installed,
        cleanup_supported=supported,
        reason=reason,
    )


def _supported_selector(selector: str) -> bool:
    """Accept only selectors using JetBrains' modern 2020.1+ Windows default roots."""

    match = _SELECTOR_RE.fullmatch(selector)
    if match is None:
        return False
    version = match.group("version").split(".")
    try:
        major = int(version[0])
        minor = int(version[1])
    except (IndexError, ValueError):
        return False
    return (major, minor) >= (2020, 1)


def _tree_stats(root: Path) -> JetBrainsTreeStats:
    logical_bytes = 0
    entry_count = 0
    latest_write_time_ns = 0
    stack = [root]

    while stack:
        path = stack.pop()
        try:
            metadata = read_file_metadata(path)
        except OSError as error:
            raise RuntimeError(f"无法读取 JetBrains 目录条目: {path}") from error
        if metadata.last_write_time_ns is None:
            raise RuntimeError(f"JetBrains 目录条目没有可验证修改时间: {path}")
        entry_count += 1
        latest_write_time_ns = max(latest_write_time_ns, metadata.last_write_time_ns)
        if not metadata.is_directory:
            logical_bytes += max(0, metadata.logical_size)
            continue
        if metadata.is_reparse_point and path != root:
            continue
        try:
            with os.scandir(path) as entries:
                stack.extend(Path(entry.path) for entry in entries)
        except OSError as error:
            raise RuntimeError(f"无法枚举 JetBrains system 目录: {path}") from error

    if entry_count <= 0 or latest_write_time_ns <= 0:
        raise RuntimeError("JetBrains system 目录没有可验证的更新时间")
    return JetBrainsTreeStats(
        logical_bytes=logical_bytes,
        entry_count=entry_count,
        latest_write_time_ns=latest_write_time_ns,
    )


def _installed_state(system_root: Path, selector: str) -> bool | None:
    """Mirror JetBrains' `.home` + product-info check, but fail closed on ambiguity."""

    locator = system_root / ".home"
    try:
        locator_meta = read_file_metadata(locator)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if locator_meta.is_directory or locator_meta.is_reparse_point:
        return None

    try:
        home_text = locator.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not home_text:
        return None
    home = Path(home_text)
    if not home.is_absolute():
        return None
    try:
        if not home.exists():
            return False
    except OSError:
        return None

    product_info = home / "product-info.json"
    try:
        info_meta = read_file_metadata(product_info)
    except FileNotFoundError:
        # JetBrains itself treats a missing product-info next to an existing
        # locator home as installed because it may be a self-built IDE.
        return True
    except OSError:
        return None
    if info_meta.is_directory or info_meta.is_reparse_point:
        return None

    try:
        value: Any = json.loads(product_info.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    name = value.get("dataDirectoryName")
    if not isinstance(name, str):
        return None
    return name == selector


def _require_no_jetbrains_process() -> None:
    clear_jetbrains_process_cache()
    if jetbrains_process_running():
        raise RuntimeError("检测到 JetBrains IDE 正在运行或进程状态无法确认；请关闭 IDE 后再清理")


def _ordinary_resolved_directory(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError(f"{label} 不能是 symlink/junction/reparse 路径: {candidate}")
        if not candidate.is_dir():
            raise ValueError(f"{label} 不存在或不是目录: {candidate}")
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 {label}: {path}") from error
    if _normalized(candidate) != _normalized(resolved):
        raise ValueError(f"{label} 存在路径重定向/reparse；只允许普通本地目录")
    metadata = read_file_metadata(candidate)
    if not metadata.is_directory or metadata.is_reparse_point:
        raise ValueError(f"{label} 不是可验证的普通目录")
    if not is_local_fixed_path(candidate):
        raise ValueError(f"{label} 不在本地固定磁盘")
    return resolved


def _directory_probe(path: Path, label: str) -> tuple[ExactDirectorySnapshot, int]:
    metadata = read_file_metadata(path)
    snapshot = _snapshot_from_metadata(metadata, label)
    if metadata.last_write_time_ns is None:
        raise RuntimeError(f"{label} 没有可验证的修改时间")
    return snapshot, metadata.last_write_time_ns


def _exact_directory_snapshot(path: Path, label: str) -> ExactDirectorySnapshot:
    return _snapshot_from_metadata(read_file_metadata(path), label)


def _snapshot_from_metadata(
    metadata: FileSystemMetadata,
    label: str,
) -> ExactDirectorySnapshot:
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.creation_time_ns is None
    ):
        raise RuntimeError(f"{label} 没有可验证的普通目录身份")
    return ExactDirectorySnapshot(
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=metadata.creation_time_ns,
    )


def _exact_root_boundary(path: Path) -> ExactRootBoundary:
    boundary = _ordinary_resolved_directory(path, "JetBrains system 父边界")
    snapshot = _exact_directory_snapshot(boundary, "JetBrains system 父边界")
    return ExactRootBoundary(
        path=boundary,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _now_ns(now: datetime | None) -> int:
    if now is None:
        return time.time_ns()
    value = now
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return int(value.timestamp() * 1_000_000_000)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


__all__ = [
    "JetBrainsLeftoverCleanupResult",
    "JetBrainsLeftoverInventory",
    "JetBrainsTreeStats",
    "cleanup_jetbrains_expired_system_directory",
    "inventory_jetbrains_expired_system_directories",
]
