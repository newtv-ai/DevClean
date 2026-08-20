"""Unity Package Manager global-cache inventory and legacy-cache cleanup."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from devclean.platform.windows.exact_cleanup import (
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_exact_directory_tree,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_DEFAULT_DB_MAX_BYTES = 10_000_000_000


class UnityUpmRootOrigin(StrEnum):
    DEFAULT = "default"
    USER_CONFIG = "user-config"
    ENVIRONMENT = "environment"


class UnityUpmStorageKind(StrEnum):
    DB = "db"
    LEGACY_PACKAGES = "packages"
    GIT_LFS = "git-lfs"


class UnityUpmLane(StrEnum):
    """Source-backed decisions for current Unity 6 Package Manager storage."""

    UNITY_MANAGED = "UNITY_MANAGED"
    USER_REVIEW = "USER_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"


@dataclass(frozen=True, slots=True)
class UnityUpmCacheRoot:
    path: Path
    origin: UnityUpmRootOrigin
    active: bool
    exists: bool


@dataclass(frozen=True, slots=True)
class UnityUpmStorageEntry:
    kind: UnityUpmStorageKind
    path: Path
    cache_root: Path | None
    logical_bytes: int
    exists: bool
    active: bool
    lane: UnityUpmLane
    deletable: bool
    reason: str


@dataclass(frozen=True, slots=True)
class UnityUpmInventory:
    roots: tuple[UnityUpmCacheRoot, ...]
    entries: tuple[UnityUpmStorageEntry, ...]
    active_root: Path
    active_db: Path
    db_max_bytes: int
    db_max_source: UnityUpmRootOrigin
    user_config_path: Path | None

    @property
    def total_visible_bytes(self) -> int:
        seen: set[str] = set()
        total = 0
        for entry in self.entries:
            key = _normalized(entry.path)
            if key in seen:
                continue
            seen.add(key)
            total += entry.logical_bytes
        return total


@dataclass(frozen=True, slots=True)
class UnityUpmLegacyCleanResult:
    cache_root: Path
    packages_path: Path
    before_bytes: int
    after_bytes: int

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


@dataclass(frozen=True, slots=True)
class _UpmLayout:
    roots: tuple[UnityUpmCacheRoot, ...]
    active_root: Path
    active_db: Path
    active_git_lfs: Path
    git_lfs_enabled: bool
    db_max_bytes: int
    db_max_source: UnityUpmRootOrigin
    user_config_path: Path | None


def inventory_unity_upm_storage(
    environment: Mapping[str, str] | None = None,
) -> UnityUpmInventory:
    """Inventory current and source-backed historical Unity UPM cache roots."""

    layout = _discover_layout(environment)
    entries: list[UnityUpmStorageEntry] = []
    seen: set[tuple[UnityUpmStorageKind, str]] = set()

    def add(
        kind: UnityUpmStorageKind,
        path: Path,
        *,
        cache_root: Path | None,
        active: bool,
        lane: UnityUpmLane,
        deletable: bool,
        reason: str,
        include_missing: bool = False,
    ) -> None:
        key = (kind, _normalized(path))
        if key in seen:
            return
        seen.add(key)
        exists = _is_plain_directory_for_inventory(path)
        if not exists and not include_missing:
            return
        entries.append(
            UnityUpmStorageEntry(
                kind=kind,
                path=path,
                cache_root=cache_root,
                logical_bytes=_directory_bytes(path) if exists else 0,
                exists=exists,
                active=active,
                lane=lane,
                deletable=deletable and exists,
                reason=reason,
            )
        )

    active_db_root = _owning_root(layout.active_db, layout.roots)
    add(
        UnityUpmStorageKind.DB,
        layout.active_db,
        cache_root=active_db_root,
        active=True,
        lane=UnityUpmLane.UNITY_MANAGED,
        deletable=False,
        reason=(
            "Unity 6 的注册表数据缓存；Package Manager 自带大小上限和 LRU 驱逐，"
            "DevClean 只统计，不与厂商垃圾收集竞争"
        ),
        include_missing=True,
    )

    for root in layout.roots:
        candidate_db = root.path / "db"
        if _normalized(candidate_db) != _normalized(layout.active_db):
            add(
                UnityUpmStorageKind.DB,
                candidate_db,
                cache_root=root.path,
                active=False,
                lane=UnityUpmLane.REPORT_ONLY,
                deletable=False,
                reason=(
                    "较低优先级或旧位置留下的 UPM 注册表缓存；可能保留离线包，"
                    "当前没有来源支持 DevClean 对它做 raw delete"
                ),
            )

        packages = root.path / "packages"
        packages_local = is_local_fixed_path(root.path) and is_local_fixed_path(packages)
        add(
            UnityUpmStorageKind.LEGACY_PACKAGES,
            packages,
            cache_root=root.path,
            active=False,
            lane=UnityUpmLane.USER_REVIEW,
            deletable=packages_local,
            reason=(
                "Unity 6 已不再使用这个旧版 packages 子目录；只有确认不再用旧版 Editor "
                "维护相关项目时才适合删除"
                if packages_local
                else "旧版 packages 位于共享、远程、可移动或 reparse 重定向边界；"
                "DevClean 只报告，不向可能影响其他用户的位置授予删除权限"
            ),
        )

        candidate_lfs = root.path / "git-lfs"
        active_lfs = layout.git_lfs_enabled and _normalized(candidate_lfs) == _normalized(
            layout.active_git_lfs
        )
        add(
            UnityUpmStorageKind.GIT_LFS,
            candidate_lfs,
            cache_root=root.path,
            active=active_lfs,
            lane=UnityUpmLane.REPORT_ONLY,
            deletable=False,
            reason=(
                "UPM Git LFS 下载缓存可减少重复下载；Unity 文档把磁盘占用与下载收益"
                "明确作为取舍，但没有给 DevClean 可复用的通用清理动作"
            ),
        )

    if _owning_root(layout.active_git_lfs, layout.roots) is None:
        add(
            UnityUpmStorageKind.GIT_LFS,
            layout.active_git_lfs,
            cache_root=None,
            active=layout.git_lfs_enabled,
            lane=UnityUpmLane.REPORT_ONLY,
            deletable=False,
            reason=(
                "UPM_GIT_LFS_CACHE_PATH 指向的 Git LFS 下载缓存；仅统计，"
                "不把自定义/共享路径转换成 raw-delete 权限"
            ),
        )

    entries.sort(
        key=lambda entry: (
            entry.kind is not UnityUpmStorageKind.LEGACY_PACKAGES,
            not entry.active,
            -entry.logical_bytes,
            str(entry.path).casefold(),
        )
    )
    return UnityUpmInventory(
        roots=layout.roots,
        entries=tuple(entries),
        active_root=layout.active_root,
        active_db=layout.active_db,
        db_max_bytes=layout.db_max_bytes,
        db_max_source=layout.db_max_source,
        user_config_path=layout.user_config_path,
    )


def delete_unity_upm_legacy_packages(
    cache_root: Path,
    environment: Mapping[str, str] | None = None,
) -> UnityUpmLegacyCleanResult:
    """Delete one exact source-backed legacy ``packages`` directory."""

    layout = _discover_layout(environment)
    root = _absolute(cache_root.expanduser())
    if not any(_normalized(root) == _normalized(item.path) for item in layout.roots):
        raise ValueError(f"不是当前可确认的 Unity UPM 缓存根: {root}")
    _require_plain_directory(root, "Unity UPM 缓存根")

    packages = root / "packages"
    _require_plain_directory(packages, "Unity UPM 旧版 packages")
    _require_local_fixed_boundary(root, packages)
    if unity_package_manager_running():
        raise RuntimeError(
            "Unity Editor、Unity Hub 或 Unity Package Manager 正在运行；"
            "请全部关闭后再删除旧版 packages 缓存"
        )

    # Re-establish the source-backed root list and exact object identities at the
    # mutation boundary. A stale UI selection cannot turn an arbitrary folder
    # named ``packages`` into deletion authority.
    layout = _discover_layout(environment)
    if not any(_normalized(root) == _normalized(item.path) for item in layout.roots):
        raise ValueError("Unity UPM 缓存配置已变化；请重新统计后再操作")
    _require_plain_directory(root, "Unity UPM 缓存根")
    _require_plain_directory(packages, "Unity UPM 旧版 packages")
    _require_local_fixed_boundary(root, packages)

    boundary = _exact_root_boundary(root)
    expected = _exact_directory_snapshot(packages, "Unity UPM 旧版 packages")
    before = _directory_bytes(packages)
    result = purge_exact_directory_tree(packages, expected, boundary)
    if not result.completed or not result.root_absent:
        raise RuntimeError("Unity UPM 旧版 packages 精确删除未完整完成")
    after = _directory_bytes(packages) if packages.is_dir() else 0
    return UnityUpmLegacyCleanResult(
        cache_root=root,
        packages_path=packages,
        before_bytes=before,
        after_bytes=after,
    )


def unity_package_manager_running() -> bool:
    """Fail closed while Unity processes that can use Package Manager are active."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -ieq 'Unity' -or "
        "$_.ProcessName -ieq 'Unity Hub' -or "
        "$_.ProcessName -ieq 'UnityHub' -or "
        "$_.ProcessName -like 'UnityPackageManager*' }; "
        "if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def _discover_layout(environment: Mapping[str, str] | None) -> _UpmLayout:
    env = _casefold_env(environment)
    config_path = _user_config_path(env)
    config = _load_user_config(config_path)

    default_root = _default_root(env)
    configured_root = _optional_absolute_path(
        config.get("cacheRoot"),
        "Unity UPM 用户配置 cacheRoot",
    )
    environment_root = _optional_environment_path(env, "upm_cache_root")

    if environment_root is not None:
        active_root = environment_root
        active_origin = UnityUpmRootOrigin.ENVIRONMENT
    elif configured_root is not None:
        active_root = configured_root
        active_origin = UnityUpmRootOrigin.USER_CONFIG
    elif default_root is not None:
        active_root = default_root
        active_origin = UnityUpmRootOrigin.DEFAULT
    else:
        raise ValueError(
            "无法确定 Unity UPM 全局缓存根: 缺少 LOCALAPPDATA 且没有 cacheRoot/UPM_CACHE_ROOT 覆盖"
        )

    root_candidates: list[tuple[Path, UnityUpmRootOrigin, bool]] = [
        (active_root, active_origin, True)
    ]
    if configured_root is not None:
        root_candidates.append(
            (
                configured_root,
                UnityUpmRootOrigin.USER_CONFIG,
                _normalized(configured_root) == _normalized(active_root),
            )
        )
    if default_root is not None:
        root_candidates.append(
            (
                default_root,
                UnityUpmRootOrigin.DEFAULT,
                _normalized(default_root) == _normalized(active_root),
            )
        )

    roots: list[UnityUpmCacheRoot] = []
    seen_roots: set[str] = set()
    for path, origin, active in root_candidates:
        key = _normalized(path)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        roots.append(
            UnityUpmCacheRoot(
                path=path,
                origin=origin,
                active=active,
                exists=_is_plain_directory_for_inventory(path),
            )
        )

    active_db = _optional_environment_path(env, "upm_npm_cache_path")
    if active_db is None:
        active_db = active_root / "db"

    db_max_value = env.get("upm_max_cache_size", "").strip()
    if db_max_value:
        db_max_bytes = _positive_int(db_max_value, "UPM_MAX_CACHE_SIZE")
        db_max_source = UnityUpmRootOrigin.ENVIRONMENT
    elif "maxCacheSize" in config:
        db_max_bytes = _positive_int(config["maxCacheSize"], "maxCacheSize")
        db_max_source = UnityUpmRootOrigin.USER_CONFIG
    else:
        db_max_bytes = _DEFAULT_DB_MAX_BYTES
        db_max_source = UnityUpmRootOrigin.DEFAULT

    configured_lfs = _optional_environment_path(env, "upm_git_lfs_cache_path")
    if configured_lfs is not None:
        active_lfs = configured_lfs
        lfs_enabled = True
    else:
        active_lfs = active_root / "git-lfs"
        lfs_enabled = bool(env.get("upm_enable_git_lfs_cache", ""))

    return _UpmLayout(
        roots=tuple(roots),
        active_root=active_root,
        active_db=active_db,
        active_git_lfs=active_lfs,
        git_lfs_enabled=lfs_enabled,
        db_max_bytes=db_max_bytes,
        db_max_source=db_max_source,
        user_config_path=config_path,
    )


def _user_config_path(env: Mapping[str, str]) -> Path | None:
    override = env.get("upm_user_config_file", "").strip()
    if override:
        return _required_absolute_path(override, "UPM_USER_CONFIG_FILE")
    profile = env.get("userprofile", "").strip()
    if not profile:
        return None
    return _absolute(Path(profile) / ".upmconfig.toml")


def _load_user_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        if not path.is_file():
            return {}
        with path.open("rb") as stream:
            value = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"无法安全读取 Unity UPM 用户配置 {path}: {error}") from error
    return value


def _default_root(env: Mapping[str, str]) -> Path | None:
    local_app_data = env.get("localappdata", "").strip()
    if not local_app_data:
        return None
    return _absolute(Path(local_app_data) / "Unity" / "cache" / "upm")


def _optional_environment_path(
    env: Mapping[str, str],
    key: str,
) -> Path | None:
    value = env.get(key, "").strip()
    return _required_absolute_path(value, key.upper()) if value else None


def _optional_absolute_path(value: object, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空绝对路径")
    return _required_absolute_path(value, label)


def _required_absolute_path(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} 必须是绝对路径: {value}")
    return _absolute(path)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是正整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as error:
            raise ValueError(f"{label} 必须是正整数") from error
    else:
        raise ValueError(f"{label} 必须是正整数")
    if parsed <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return parsed


def _owning_root(path: Path, roots: tuple[UnityUpmCacheRoot, ...]) -> Path | None:
    for root in roots:
        try:
            common = os.path.commonpath((str(root.path), str(path)))
        except ValueError:
            continue
        if _normalized(Path(common)) == _normalized(root.path):
            return root.path
    return None


def _is_plain_directory_for_inventory(path: Path) -> bool:
    try:
        return path.is_dir() and not path.is_symlink() and not path.is_junction()
    except OSError:
        return False


def _require_plain_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"{label}不存在: {path}")
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"拒绝把链接或 junction 作为{label}删除边界: {path}")


def _require_local_fixed_boundary(root: Path, packages: Path) -> None:
    if not is_local_fixed_path(root) or not is_local_fixed_path(packages):
        raise ValueError(
            "旧版 UPM packages 只允许在本地固定磁盘上删除；共享、远程、可移动或"
            "经过 reparse 重定向的缓存只报告，不获得删除权限"
        )


def _exact_directory_snapshot(path: Path, label: str) -> ExactDirectorySnapshot:
    metadata = read_file_metadata(path)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.creation_time_ns is None
    ):
        raise RuntimeError(f"{label}没有可验证的普通目录身份")
    return ExactDirectorySnapshot(
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=metadata.creation_time_ns,
    )


def _exact_root_boundary(path: Path) -> ExactRootBoundary:
    snapshot = _exact_directory_snapshot(path, "Unity UPM 缓存根")
    return ExactRootBoundary(
        path=path,
        volume_serial=snapshot.volume_serial,
        file_id=snapshot.file_id,
        file_id_kind=snapshot.file_id_kind,
    )


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            safe_subdirs: list[str] = []
            for name in subdirs:
                child = base / name
                try:
                    if child.is_symlink() or child.is_junction():
                        continue
                except OSError:
                    continue
                safe_subdirs.append(name)
            subdirs[:] = safe_subdirs
            for name in files:
                path = base / name
                try:
                    if path.is_symlink():
                        continue
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "UnityUpmCacheRoot",
    "UnityUpmInventory",
    "UnityUpmLane",
    "UnityUpmLegacyCleanResult",
    "UnityUpmRootOrigin",
    "UnityUpmStorageEntry",
    "UnityUpmStorageKind",
    "delete_unity_upm_legacy_packages",
    "inventory_unity_upm_storage",
    "unity_package_manager_running",
]
