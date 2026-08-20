"""Exact npm cache inventory and vendor-supported maintenance.

Package content cache GC/cleaning and npx entry removal stay behind npm's own
commands. DevClean never removes cacache/TUF internals directly and never uses
an abbreviated npx key as deletion authority.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core.npm_cleanup import clear_npm_process_cache, npm_process_running
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path


@dataclass(frozen=True, slots=True)
class NpmPathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    is_directory: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class NpmCacheArea:
    path: Path
    exists: bool
    logical_bytes: int
    file_count: int


@dataclass(frozen=True, slots=True)
class NpmNpxEntry:
    key: str
    path: Path
    description: str
    logical_bytes: int
    file_count: int


@dataclass(frozen=True, slots=True)
class NpmStorageInventory:
    npm_tool: NpmPathIdentity
    cache_root: Path
    cache_root_identity: NpmPathIdentity | None
    content_cache: NpmCacheArea
    npx_cache: NpmCacheArea
    tuf_cache: NpmCacheArea
    content_keys: tuple[str, ...]
    npx_entries: tuple[NpmNpxEntry, ...]
    warnings: tuple[str, ...]

    @property
    def total_cache_bytes(self) -> int:
        return (
            self.content_cache.logical_bytes
            + self.npx_cache.logical_bytes
            + self.tuf_cache.logical_bytes
        )


@dataclass(frozen=True, slots=True)
class NpmCacheVerifyResult:
    cache_path: Path
    before_bytes: int
    after_bytes: int
    before_keys: int
    after_keys: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


@dataclass(frozen=True, slots=True)
class NpmContentCleanResult:
    cache_path: Path
    before_bytes: int
    after_bytes: int
    removed_keys: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


@dataclass(frozen=True, slots=True)
class NpmNpxRemoveResult:
    key: str
    path: Path
    before_bytes: int
    after_bytes: int
    stdout: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_npm_storage(
    environment: Mapping[str, str] | None = None,
) -> NpmStorageInventory:
    """Inventory the exact configured npm cache and vendor-visible cache objects."""

    tool = _resolve_npm_tool(environment)
    discovery_env = _base_environment(environment)
    cache_root = _discover_cache_root(tool, discovery_env)
    pinned_env = _npm_environment(cache_root, environment)

    # The same executable must confirm the pinned value. This catches config/env
    # surprises before any object is represented as belonging to this root.
    confirmed = _discover_cache_root(tool, pinned_env)
    if _normalize(confirmed) != _normalize(cache_root):
        raise RuntimeError("npm 未确认固定的 cache 根目录; 已安全停止")

    root_identity = _optional_root_identity(cache_root)
    content_path = cache_root / "_cacache"
    npx_path = cache_root / "_npx"
    tuf_path = cache_root / "_tuf"

    first_keys = _list_content_keys(tool, pinned_env)
    npx_rows = _list_npx_entries(tool, cache_root, pinned_env)
    second_keys = _list_content_keys(tool, pinned_env)
    if first_keys != second_keys:
        raise RuntimeError("npm package cache 在 inventory 期间发生变化; 请重新检查")

    content_area = _measure_area(content_path)
    npx_area = _measure_area(npx_path)
    tuf_area = _measure_area(tuf_path)
    warnings: list[str] = []
    if content_area.exists and content_area.file_count == 0 and first_keys:
        warnings.append("npm cache ls 返回内容键，但 _cacache 文件统计为空；结果仅用于说明。")

    entries: list[NpmNpxEntry] = []
    for key, description in npx_rows:
        entry_path = npx_path / key
        area = _measure_area(entry_path)
        if not area.exists:
            raise RuntimeError(f"npm cache npx ls 返回的 entry 在磁盘上不存在: {key}")
        entries.append(
            NpmNpxEntry(
                key=key,
                path=entry_path,
                description=description,
                logical_bytes=area.logical_bytes,
                file_count=area.file_count,
            )
        )
    entries.sort(key=lambda item: item.key.casefold())

    fresh_tool = _path_identity(tool.path, expect_directory=False, label="npm CLI")
    if fresh_tool != tool:
        raise RuntimeError("npm CLI 身份在 inventory 期间发生变化")
    if root_identity is not None:
        fresh_root = _path_identity(cache_root, expect_directory=True, label="npm cache root")
        if fresh_root != root_identity:
            raise RuntimeError("npm cache root 身份在 inventory 期间发生变化")

    return NpmStorageInventory(
        npm_tool=tool,
        cache_root=cache_root,
        cache_root_identity=root_identity,
        content_cache=content_area,
        npx_cache=npx_area,
        tuf_cache=tuf_area,
        content_keys=first_keys,
        npx_entries=tuple(entries),
        warnings=tuple(warnings),
    )


def verify_npm_content_cache(
    reviewed: NpmStorageInventory,
    environment: Mapping[str, str] | None = None,
) -> NpmCacheVerifyResult:
    """Run npm's deterministic integrity check/GC on the reviewed package cache."""

    current = _validated_current_inventory(reviewed, environment)
    _require_process_idle()
    before = current.content_cache.logical_bytes
    before_keys = len(current.content_keys)
    result = _run_npm(
        current.npm_tool,
        ("cache", "verify"),
        _npm_environment(current.cache_root, environment),
        timeout=600,
    )
    _require_success(result, "npm cache verify")

    after = inventory_npm_storage(environment)
    _require_same_boundaries(current, after)
    return NpmCacheVerifyResult(
        cache_path=current.content_cache.path,
        before_bytes=before,
        after_bytes=after.content_cache.logical_bytes,
        before_keys=before_keys,
        after_keys=len(after.content_keys),
        stdout=_combined_output(result.stdout, result.stderr),
    )


def clean_npm_content_cache(
    reviewed: NpmStorageInventory,
    environment: Mapping[str, str] | None = None,
) -> NpmContentCleanResult:
    """Clear the exact reviewed ``_cacache`` through ``npm cache clean --force``."""

    current = _validated_current_inventory(reviewed, environment)
    if (
        current.content_keys != reviewed.content_keys
        or current.content_cache.logical_bytes != reviewed.content_cache.logical_bytes
        or current.content_cache.file_count != reviewed.content_cache.file_count
    ):
        raise RuntimeError("npm package cache 自审核后已变化; 请重新统计并确认后再清空")
    _require_process_idle()
    before = current.content_cache.logical_bytes
    removed_keys = len(current.content_keys)
    result = _run_npm(
        current.npm_tool,
        ("cache", "clean", "--force"),
        _npm_environment(current.cache_root, environment),
        timeout=600,
    )
    _require_success(result, "npm cache clean --force")

    after = inventory_npm_storage(environment)
    _require_same_boundaries(current, after)
    if after.content_keys or after.content_cache.logical_bytes != 0:
        raise RuntimeError("npm 返回成功后 package cache 仍包含内容; 不报告清空成功")
    return NpmContentCleanResult(
        cache_path=current.content_cache.path,
        before_bytes=before,
        after_bytes=after.content_cache.logical_bytes,
        removed_keys=removed_keys,
        stdout=_combined_output(result.stdout, result.stderr),
    )


def remove_npm_npx_entry(
    reviewed: NpmStorageInventory,
    expected: NpmNpxEntry,
    environment: Mapping[str, str] | None = None,
) -> NpmNpxRemoveResult:
    """Remove one exact full vendor-listed npx cache key after dry-run path proof."""

    current = _validated_current_inventory(reviewed, environment)
    entry = _require_same_npx_entry(current, expected)
    _require_process_idle()
    env = _npm_environment(current.cache_root, environment)

    preview = _run_npm(
        current.npm_tool,
        ("cache", "npx", "rm", entry.key, "--dry-run"),
        env,
        timeout=120,
    )
    _require_success(preview, "npm cache npx rm --dry-run")
    preview_path = _parse_npx_remove_path(preview.stdout)
    if preview_path is None or _normalize(preview_path) != _normalize(entry.path):
        raise RuntimeError("npm npx dry-run 删除路径与审核 entry 不一致; 拒绝执行")

    # Revalidate after dry-run so a cache/config change cannot redirect the real action.
    fresh = _validated_current_inventory(current, environment)
    entry = _require_same_npx_entry(fresh, entry)
    _require_process_idle()
    fresh_preview = _run_npm(
        fresh.npm_tool,
        ("cache", "npx", "rm", entry.key, "--dry-run"),
        _npm_environment(fresh.cache_root, environment),
        timeout=120,
    )
    _require_success(fresh_preview, "npm cache npx rm --dry-run")
    fresh_path = _parse_npx_remove_path(fresh_preview.stdout)
    if fresh_path is None or _normalize(fresh_path) != _normalize(entry.path):
        raise RuntimeError("npm npx fresh dry-run 范围已变化; 请重新检查")

    before = entry.logical_bytes
    result = _run_npm(
        fresh.npm_tool,
        ("cache", "npx", "rm", entry.key),
        _npm_environment(fresh.cache_root, environment),
        timeout=600,
    )
    _require_success(result, "npm cache npx rm")

    after = inventory_npm_storage(environment)
    _require_same_boundaries(fresh, after)
    if any(item.key == entry.key for item in after.npx_entries):
        raise RuntimeError("npm 返回成功后目标 npx entry 仍然存在; 不报告成功")
    return NpmNpxRemoveResult(
        key=entry.key,
        path=entry.path,
        before_bytes=before,
        after_bytes=0,
        stdout=_combined_output(result.stdout, result.stderr),
    )


def _validated_current_inventory(
    reviewed: NpmStorageInventory,
    environment: Mapping[str, str] | None,
) -> NpmStorageInventory:
    current = inventory_npm_storage(environment)
    _require_same_boundaries(reviewed, current)
    return current


def _require_same_boundaries(
    reviewed: NpmStorageInventory,
    current: NpmStorageInventory,
) -> None:
    if reviewed.npm_tool != current.npm_tool:
        raise RuntimeError("npm CLI 身份自审核后发生变化")
    if _normalize(reviewed.cache_root) != _normalize(current.cache_root):
        raise RuntimeError("npm cache 根目录自审核后发生变化")
    if reviewed.cache_root_identity != current.cache_root_identity:
        raise RuntimeError("npm cache 根目录身份自审核后发生变化")


def _require_same_npx_entry(
    inventory: NpmStorageInventory,
    expected: NpmNpxEntry,
) -> NpmNpxEntry:
    matches = [item for item in inventory.npx_entries if item.key == expected.key]
    if len(matches) != 1:
        raise RuntimeError("审核的 npx entry 已不存在或不再唯一")
    current = matches[0]
    if current != expected:
        raise RuntimeError("审核的 npx entry 内容/大小/路径已变化; 请重新检查")
    return current


def _discover_cache_root(tool: NpmPathIdentity, environment: Mapping[str, str]) -> Path:
    result = _run_npm(tool, ("config", "get", "cache"), environment, timeout=30)
    _require_success(result, "npm config get cache")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("npm config get cache 没有返回路径")
    raw = lines[-1].strip().strip('"').strip("'")
    candidate = PureWindowsPath(raw)
    if not candidate.is_absolute():
        # Keep portable tests and non-Windows development usable while production
        # Windows values still require an absolute vendor path.
        native = Path(raw).expanduser()
        if not native.is_absolute():
            raise RuntimeError(f"npm 返回的 cache 路径不是绝对路径: {raw}")
        return Path(os.path.abspath(native))
    return Path(str(candidate))


def _list_content_keys(
    tool: NpmPathIdentity,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    result = _run_npm(tool, ("cache", "ls"), environment, timeout=180)
    _require_success(result, "npm cache ls")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _list_npx_entries(
    tool: NpmPathIdentity,
    cache_root: Path,
    environment: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    result = _run_npm(tool, ("cache", "npx", "ls"), environment, timeout=180)
    _require_success(result, "npm cache npx ls")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if lines == ["npx cache does not exist"]:
        return ()

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    npx_root = cache_root / "_npx"
    for line in lines:
        key, separator, description = line.partition(":")
        key = key.strip()
        if not separator or not _valid_npx_key(key):
            raise RuntimeError(f"无法安全解析 npm cache npx ls 行: {line}")
        if key in seen:
            raise RuntimeError(f"npm cache npx ls 返回重复 key: {key}")
        seen.add(key)
        path = npx_root / key
        if _normalize(path.parent) != _normalize(npx_root):
            raise RuntimeError(f"npx key 会逃逸 cache 根目录: {key}")
        rows.append((key, description.strip()))
    return tuple(rows)


def _valid_npx_key(key: str) -> bool:
    if not key or key in {".", ".."} or "\x00" in key:
        return False
    if any(character in key for character in ("/", "\\", ":")):
        return False
    return Path(key).name == key


def _parse_npx_remove_path(stdout: str) -> Path | None:
    prefix = "Removing npx key at "
    matches = [
        line.strip()[len(prefix) :].strip().strip('"').strip("'")
        for line in stdout.splitlines()
        if line.strip().startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        return None
    raw = matches[0]
    candidate = PureWindowsPath(raw)
    if candidate.is_absolute():
        return Path(str(candidate))
    native = Path(raw)
    return native if native.is_absolute() else None


def _resolve_npm_tool(environment: Mapping[str, str] | None) -> NpmPathIdentity:
    source = os.environ if environment is None else environment
    folded = {str(key).casefold(): str(value) for key, value in source.items() if value}
    raw = folded.get("devclean_npm_exe") or ("npm.cmd" if os.name == "nt" else "npm")
    candidate = Path(raw)
    if not candidate.is_absolute():
        resolved = shutil.which(raw, path=folded.get("path"))
        if resolved is None:
            raise FileNotFoundError("未找到 npm CLI")
        candidate = Path(resolved)
    return _path_identity(candidate, expect_directory=False, label="npm CLI")


def _optional_root_identity(path: Path) -> NpmPathIdentity | None:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    if not exists:
        return None
    return _path_identity(path, expect_directory=True, label="npm cache root")


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> NpmPathIdentity:
    candidate = Path(os.path.abspath(path.expanduser()))
    if candidate.is_symlink() or candidate.is_junction():
        raise RuntimeError(f"{label} 不能是 symlink/junction/reparse")
    resolved = candidate.resolve(strict=True)
    if os.path.normcase(os.path.abspath(candidate)) != os.path.normcase(os.path.abspath(resolved)):
        raise RuntimeError(f"{label} 路径包含重定向/reparse")
    if not is_local_fixed_path(resolved):
        raise RuntimeError(f"{label} 不在本地固定磁盘")
    metadata = read_file_metadata(resolved)
    if metadata.is_directory != expect_directory:
        raise RuntimeError(f"{label} 类型与预期不一致")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise RuntimeError(f"{label} 是 reparse/cloud placeholder; 不授予维护权限")
    if metadata.volume_serial is None or metadata.file_id is None or metadata.file_id_kind is None:
        raise RuntimeError(f"{label} 缺少稳定文件身份")
    return NpmPathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        is_directory=metadata.is_directory,
        creation_time_ns=None if expect_directory else metadata.creation_time_ns,
        last_write_time_ns=None if expect_directory else metadata.last_write_time_ns,
    )


def _measure_area(path: Path) -> NpmCacheArea:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    if not exists:
        return NpmCacheArea(path, False, 0, 0)
    logical_bytes, file_count = _directory_stats(path)
    return NpmCacheArea(path, True, logical_bytes, file_count)


def _directory_stats(root: Path) -> tuple[int, int]:
    total = 0
    files_seen = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            kept: list[str] = []
            for name in subdirs:
                child = base / name
                try:
                    if child.is_symlink() or child.is_junction():
                        continue
                except OSError:
                    continue
                kept.append(name)
            subdirs[:] = kept
            for name in files:
                child = base / name
                try:
                    result = os.stat(child, follow_symlinks=False)
                except OSError:
                    continue
                total += max(0, int(result.st_size))
                files_seen += 1
    except OSError:
        return total, files_seen
    return total, files_seen


def _run_npm(
    tool: NpmPathIdentity,
    arguments: Sequence[str],
    environment: Mapping[str, str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(tool.path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=dict(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 npm: {error}") from error


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode == 0:
        return
    detail = _combined_output(result.stdout, result.stderr)
    raise RuntimeError(f"{label} 失败 (退出码 {result.returncode}): {detail or 'no output'}")


def _base_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    return env


def _npm_environment(
    cache_root: Path,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    env = _base_environment(environment)
    for key in tuple(env):
        if key.casefold() == "npm_config_cache":
            del env[key]
    env["NPM_CONFIG_CACHE"] = str(cache_root)
    env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    return env


def _require_process_idle() -> None:
    clear_npm_process_cache()
    if npm_process_running():
        raise RuntimeError("npm/npx 正在运行或进程状态无法确认; 拒绝修改 npm cache")


def _normalize(path: str | os.PathLike[str]) -> str:
    return _impl._normalize(path)


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip())


__all__ = [
    "NpmCacheArea",
    "NpmCacheVerifyResult",
    "NpmContentCleanResult",
    "NpmNpxEntry",
    "NpmNpxRemoveResult",
    "NpmPathIdentity",
    "NpmStorageInventory",
    "clean_npm_content_cache",
    "inventory_npm_storage",
    "remove_npm_npx_entry",
    "verify_npm_content_cache",
]
