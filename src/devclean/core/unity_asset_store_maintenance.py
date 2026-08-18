"""Unity Asset Store package-cache inventory and exact user-directed removal."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.platform.windows.exact_cleanup import (
    ExactFileSnapshot,
    ExactRootBoundary,
    purge_exact_file,
)
from devclean.platform.windows.filesystem import read_file_metadata

_ASSET_STORE_DIR = "Asset Store-5.x"


class UnityAssetStoreRootOrigin(StrEnum):
    DEFAULT = "default"
    ENVIRONMENT = "environment"
    USER_SELECTED = "user-selected"


@dataclass(frozen=True, slots=True)
class UnityAssetStoreCacheRoot:
    path: Path
    origin: UnityAssetStoreRootOrigin
    exists: bool
    package_count: int
    package_bytes: int


@dataclass(frozen=True, slots=True)
class UnityAssetStorePackage:
    cache_root: Path
    path: Path
    relative_path: Path
    publisher: str
    logical_bytes: int
    origin: UnityAssetStoreRootOrigin
    user_review_required: bool = True


@dataclass(frozen=True, slots=True)
class UnityAssetStoreInventory:
    roots: tuple[UnityAssetStoreCacheRoot, ...]
    packages: tuple[UnityAssetStorePackage, ...]

    @property
    def package_bytes(self) -> int:
        return sum(package.logical_bytes for package in self.packages)


@dataclass(frozen=True, slots=True)
class UnityAssetStoreDeleteResult:
    cache_root: Path
    package_path: Path
    before_bytes: int
    path_absent: bool

    @property
    def reclaimed_bytes(self) -> int:
        return self.before_bytes if self.path_absent else 0


def inventory_unity_asset_store(
    environment: Mapping[str, str] | None = None,
    extra_locations: Sequence[Path] = (),
) -> UnityAssetStoreInventory:
    """Inventory exact .unitypackage files from known or user-supplied cache roots."""

    roots = _candidate_roots(environment, extra_locations)
    root_entries: list[UnityAssetStoreCacheRoot] = []
    packages: list[UnityAssetStorePackage] = []
    for root, origin in roots:
        try:
            exists = root.is_dir()
        except OSError:
            exists = False
        found = _packages_under(root, origin) if exists else ()
        root_entries.append(
            UnityAssetStoreCacheRoot(
                path=root,
                origin=origin,
                exists=exists,
                package_count=len(found),
                package_bytes=sum(package.logical_bytes for package in found),
            )
        )
        packages.extend(found)
    packages.sort(key=lambda package: package.logical_bytes, reverse=True)
    return UnityAssetStoreInventory(tuple(root_entries), tuple(packages))


def delete_unity_asset_store_package(
    cache_root: Path,
    package_path: Path,
) -> UnityAssetStoreDeleteResult:
    """Delete one exact .unitypackage inside one explicitly approved cache root."""

    root = _validated_cache_root(cache_root)
    package = _validated_package(root, package_path)
    boundary = _exact_root_boundary(root)
    expected = _exact_file_snapshot(package)
    result = purge_exact_file(package, expected, boundary)
    if not result.source_name_absent:
        raise RuntimeError(
            "所选 Unity 资源包的原路径已被并发替换; 精确对象已处理但不能报告路径已清空"
        )
    return UnityAssetStoreDeleteResult(
        cache_root=root,
        package_path=package,
        before_bytes=expected.logical_size,
        path_absent=True,
    )


def _candidate_roots(
    environment: Mapping[str, str] | None,
    extra_locations: Sequence[Path],
) -> tuple[tuple[Path, UnityAssetStoreRootOrigin], ...]:
    env = _casefold_env(environment)
    candidates: list[tuple[Path, UnityAssetStoreRootOrigin]] = []

    appdata = env.get("appdata")
    if appdata:
        candidates.append(
            (
                _absolute(Path(appdata) / "Unity" / _ASSET_STORE_DIR),
                UnityAssetStoreRootOrigin.DEFAULT,
            )
        )

    override = env.get("assetstore_cache_path")
    if override:
        candidates.append(
            (
                _cache_dir_from_location(Path(override)),
                UnityAssetStoreRootOrigin.ENVIRONMENT,
            )
        )

    candidates.extend(
        (
            _cache_dir_from_location(path),
            UnityAssetStoreRootOrigin.USER_SELECTED,
        )
        for path in extra_locations
    )

    deduplicated: list[tuple[Path, UnityAssetStoreRootOrigin]] = []
    seen: set[str] = set()
    for path, origin in candidates:
        key = _normalized(path)
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append((path, origin))
    return tuple(deduplicated)


def _cache_dir_from_location(location: Path) -> Path:
    path = _absolute(location.expanduser())
    if path.name.casefold() == _ASSET_STORE_DIR.casefold():
        return path
    return path / _ASSET_STORE_DIR


def _packages_under(
    root: Path,
    origin: UnityAssetStoreRootOrigin,
) -> tuple[UnityAssetStorePackage, ...]:
    found: list[UnityAssetStorePackage] = []
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
                if not name.casefold().endswith(".unitypackage"):
                    continue
                path = base / name
                try:
                    if path.is_symlink():
                        continue
                    size = path.stat().st_size
                    relative = path.relative_to(root)
                except (OSError, ValueError):
                    continue
                publisher = relative.parts[0] if len(relative.parts) > 1 else "(root)"
                found.append(
                    UnityAssetStorePackage(
                        cache_root=root,
                        path=path,
                        relative_path=relative,
                        publisher=publisher,
                        logical_bytes=size,
                        origin=origin,
                    )
                )
    except OSError:
        return tuple(found)
    return tuple(found)


def _validated_cache_root(cache_root: Path) -> Path:
    root = _absolute(cache_root.expanduser())
    if root.name.casefold() != _ASSET_STORE_DIR.casefold():
        raise ValueError(f"不是 Unity Asset Store 缓存目录: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"Unity Asset Store 缓存目录不存在: {root}")
    if root.is_symlink() or root.is_junction():
        raise ValueError(f"拒绝把链接或 junction 作为 Unity Asset Store 删除边界: {root}")
    return root


def _validated_package(cache_root: Path, package_path: Path) -> Path:
    package = _absolute(package_path.expanduser())
    try:
        common = os.path.commonpath((str(cache_root), str(package)))
    except ValueError as error:
        raise ValueError("所选资源包不在已批准的 Unity Asset Store 缓存中") from error
    if _normalized(Path(common)) != _normalized(cache_root) or package == cache_root:
        raise ValueError("所选资源包不在已批准的 Unity Asset Store 缓存中")
    if package.suffix.casefold() != ".unitypackage":
        raise ValueError(f"只允许删除 .unitypackage 缓存文件: {package}")
    if not package.is_file():
        raise FileNotFoundError(f"Unity Asset Store 资源包不存在: {package}")
    if package.is_symlink():
        raise ValueError(f"拒绝删除链接形式的 Unity Asset Store 资源包: {package}")
    return package


def _exact_root_boundary(path: Path) -> ExactRootBoundary:
    metadata = read_file_metadata(path)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError("Unity Asset Store 缓存根没有可验证的普通目录身份")
    return ExactRootBoundary(
        path=path,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
    )


def _exact_file_snapshot(path: Path) -> ExactFileSnapshot:
    metadata = read_file_metadata(path)
    if (
        metadata.is_directory
        or metadata.is_reparse_point
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.link_count is None
        or metadata.attributes is None
        or metadata.creation_time_ns is None
        or metadata.last_write_time_ns is None
    ):
        raise RuntimeError("Unity Asset Store 资源包没有可验证的普通文件身份")
    if metadata.link_count != 1:
        raise RuntimeError("拒绝删除硬链接形式的 Unity Asset Store 资源包")
    return ExactFileSnapshot(
        logical_size=metadata.logical_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        reparse_tag=metadata.reparse_tag,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "UnityAssetStoreCacheRoot",
    "UnityAssetStoreDeleteResult",
    "UnityAssetStoreInventory",
    "UnityAssetStorePackage",
    "UnityAssetStoreRootOrigin",
    "delete_unity_asset_store_package",
    "inventory_unity_asset_store",
]
