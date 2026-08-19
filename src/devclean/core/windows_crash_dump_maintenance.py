"""Source-backed Windows crash-dump inventory and exact USER_REVIEW removal."""

from __future__ import annotations

import ctypes
import os
import winreg
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.platform.windows.exact_cleanup import (
    ExactFileSnapshot,
    ExactMutationResult,
    ExactRootBoundary,
    purge_exact_file,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_CRASH_CONTROL_KEY = r"SYSTEM\CurrentControlSet\Control\CrashControl"
_LOCAL_DUMPS_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
_DEFAULT_DUMP_FILE = r"%SystemRoot%\MEMORY.DMP"
_DEFAULT_MINIDUMP_DIR = r"%SystemRoot%\Minidump"
_DEFAULT_LOCAL_DUMP_DIR = r"%LOCALAPPDATA%\CrashDumps"
_WINDOWS = os.name == "nt"


class WindowsCrashDumpKind(StrEnum):
    KERNEL_MEMORY = "kernel-memory"
    KERNEL_SMALL = "kernel-small"
    USER_MODE = "user-mode"


@dataclass(frozen=True, slots=True)
class WindowsCrashDumpLocation:
    kind: WindowsCrashDumpKind
    path: Path
    direct_file: bool
    source: str
    configured_for: tuple[str, ...]
    requires_elevation: bool


@dataclass(frozen=True, slots=True)
class WindowsCrashDumpEntry:
    kind: WindowsCrashDumpKind
    path: Path
    root: Path
    logical_bytes: int
    creation_time_ns: int
    last_write_time_ns: int
    source: str
    configured_for: tuple[str, ...]
    requires_elevation: bool
    deletion_supported: bool
    reason: str
    root_boundary: ExactRootBoundary | None
    snapshot: ExactFileSnapshot | None


@dataclass(frozen=True, slots=True)
class WindowsCrashDumpInventory:
    elevated: bool
    locations: tuple[WindowsCrashDumpLocation, ...]
    entries: tuple[WindowsCrashDumpEntry, ...]
    warnings: tuple[str, ...]

    @property
    def logical_bytes(self) -> int:
        return sum(entry.logical_bytes for entry in self.entries)


@dataclass(frozen=True, slots=True)
class WindowsCrashDumpDeleteResult:
    entry: WindowsCrashDumpEntry
    mutation: ExactMutationResult

    @property
    def logical_bytes_removed(self) -> int:
        return self.entry.logical_bytes if self.mutation.source_name_absent else 0


def inventory_windows_crash_dumps(
    environment: Mapping[str, str] | None = None,
) -> WindowsCrashDumpInventory:
    """Inventory exact configured Windows crash-dump artifacts without mutation."""

    if not _WINDOWS:
        raise RuntimeError("Windows 崩溃转储维护仅支持 Windows")
    elevated = _is_process_elevated()
    locations, warnings = _discover_locations(environment)
    entries: list[WindowsCrashDumpEntry] = []
    for location in locations:
        entries.extend(_entries_for_location(location, elevated))
    entries.sort(key=lambda item: item.logical_bytes, reverse=True)
    return WindowsCrashDumpInventory(
        elevated=elevated,
        locations=locations,
        entries=tuple(entries),
        warnings=warnings,
    )


def delete_windows_crash_dump(
    expected: WindowsCrashDumpEntry,
    environment: Mapping[str, str] | None = None,
) -> WindowsCrashDumpDeleteResult:
    """Permanently remove one reviewed dump after a fresh source/identity check."""

    if not _WINDOWS:
        raise RuntimeError("Windows 崩溃转储维护仅支持 Windows")
    if not expected.deletion_supported:
        raise ValueError(expected.reason)
    if expected.requires_elevation and not _is_process_elevated():
        raise PermissionError("内核崩溃转储需要管理员权限；DevClean 不会自动提升权限")

    fresh = inventory_windows_crash_dumps(environment)
    matches = [
        item
        for item in fresh.entries
        if item.kind == expected.kind and _normalized(item.path) == _normalized(expected.path)
    ]
    if len(matches) != 1:
        raise RuntimeError("无法唯一重新确认所选 Windows 崩溃转储；请重新检查")
    current = matches[0]
    _require_same_entry(expected, current)
    if not current.deletion_supported or current.snapshot is None or current.root_boundary is None:
        raise RuntimeError(current.reason)

    mutation = purge_exact_file(current.path, current.snapshot, current.root_boundary)
    if not mutation.source_name_absent:
        raise RuntimeError("精确崩溃转储对象已处理，但原路径被并发替换；不能报告清理成功")
    return WindowsCrashDumpDeleteResult(entry=current, mutation=mutation)


def _entries_for_location(
    location: WindowsCrashDumpLocation,
    elevated: bool,
) -> tuple[WindowsCrashDumpEntry, ...]:
    if location.direct_file:
        path = _absolute(location.path)
        if not path.exists():
            return ()
        entry = _entry_from_file(location, path, path.parent, elevated)
        return (entry,) if entry is not None else ()

    root = _absolute(location.path)
    try:
        if not root.is_dir():
            return ()
    except OSError:
        return ()
    found: list[WindowsCrashDumpEntry] = []
    try:
        with os.scandir(root) as scan:
            for child in scan:
                if not child.name.casefold().endswith(".dmp"):
                    continue
                try:
                    if not child.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                entry = _entry_from_file(location, Path(child.path), root, elevated)
                if entry is not None:
                    found.append(entry)
    except OSError:
        return ()
    return tuple(found)


def _entry_from_file(
    location: WindowsCrashDumpLocation,
    path: Path,
    root: Path,
    elevated: bool,
) -> WindowsCrashDumpEntry | None:
    try:
        root_boundary = _root_boundary(root)
        snapshot = _file_snapshot(path)
    except (OSError, RuntimeError, ValueError):
        return _report_only_entry(location, path, root, "路径或文件身份无法安全验证")

    if location.requires_elevation and not elevated:
        supported = False
        reason = "内核崩溃转储属于 USER_REVIEW，但删除需要管理员权限；DevClean 不会自动提升"
    else:
        supported = True
        reason = (
            "崩溃转储是诊断证据；技术身份已确认，但是否仍需用于 WinDbg/支持分析由用户决定"
        )
    return WindowsCrashDumpEntry(
        kind=location.kind,
        path=_absolute(path),
        root=_absolute(root),
        logical_bytes=snapshot.logical_size,
        creation_time_ns=snapshot.creation_time_ns,
        last_write_time_ns=snapshot.last_write_time_ns,
        source=location.source,
        configured_for=location.configured_for,
        requires_elevation=location.requires_elevation,
        deletion_supported=supported,
        reason=reason,
        root_boundary=root_boundary,
        snapshot=snapshot,
    )


def _report_only_entry(
    location: WindowsCrashDumpLocation,
    path: Path,
    root: Path,
    reason: str,
) -> WindowsCrashDumpEntry | None:
    try:
        metadata = read_file_metadata(path)
    except OSError:
        return None
    if metadata.is_directory:
        return None
    return WindowsCrashDumpEntry(
        kind=location.kind,
        path=_absolute(path),
        root=_absolute(root),
        logical_bytes=max(0, metadata.logical_size),
        creation_time_ns=metadata.creation_time_ns or 0,
        last_write_time_ns=metadata.last_write_time_ns or 0,
        source=location.source,
        configured_for=location.configured_for,
        requires_elevation=location.requires_elevation,
        deletion_supported=False,
        reason=reason,
        root_boundary=None,
        snapshot=None,
    )


def _discover_locations(
    environment: Mapping[str, str] | None,
) -> tuple[tuple[WindowsCrashDumpLocation, ...], tuple[str, ...]]:
    env = _casefold_env(environment)
    locations: list[WindowsCrashDumpLocation] = []
    warnings: list[str] = []

    crash_locations, crash_warnings = _discover_crash_control_locations(env)
    local_locations, local_warnings = _discover_local_dump_locations(env)
    locations.extend(crash_locations)
    locations.extend(local_locations)
    warnings.extend(crash_warnings)
    warnings.extend(local_warnings)

    deduped: list[WindowsCrashDumpLocation] = []
    seen: set[tuple[WindowsCrashDumpKind, str, bool]] = set()
    for location in locations:
        key = (location.kind, _normalized(location.path), location.direct_file)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(location)
    return tuple(deduped), tuple(warnings)


def _discover_crash_control_locations(
    env: Mapping[str, str],
) -> tuple[tuple[WindowsCrashDumpLocation, ...], tuple[str, ...]]:
    warnings: list[str] = []
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _CRASH_CONTROL_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return (), ("CrashControl 注册表项不存在；不猜测内核转储位置",)
    except OSError:
        return (), ("无法读取 CrashControl 注册表项；内核转储只报告为未审计位置",)

    try:
        dump_raw, dump_ok = _optional_registry_string(key, "DumpFile")
        mini_raw, mini_ok = _optional_registry_string(key, "MinidumpDir")
        mode, mode_ok = _optional_registry_dword(key, "CrashDumpEnabled")
        filter_pages, filter_ok = _optional_registry_dword(key, "FilterPages")
    finally:
        winreg.CloseKey(key)

    if not dump_ok or not mini_ok or not mode_ok or not filter_ok:
        return (), ("CrashControl 路径/模式字段类型异常；拒绝从不完整配置获得删除权限",)

    dump_path = _resolve_crash_control_path(dump_raw or _DEFAULT_DUMP_FILE, env)
    mini_path = _resolve_crash_control_path(mini_raw or _DEFAULT_MINIDUMP_DIR, env)
    mode_label = _crash_mode_label(mode, filter_pages)
    found: list[WindowsCrashDumpLocation] = []
    if dump_path is None:
        warnings.append("CrashControl DumpFile 含不支持的环境变量或不是本地绝对路径")
    else:
        found.append(
            WindowsCrashDumpLocation(
                kind=WindowsCrashDumpKind.KERNEL_MEMORY,
                path=dump_path,
                direct_file=True,
                source=f"CrashControl DumpFile（当前模式：{mode_label}）",
                configured_for=("系统内核/完整/自动/活动内存转储",),
                requires_elevation=True,
            )
        )
    if mini_path is None:
        warnings.append("CrashControl MinidumpDir 含不支持的环境变量或不是本地绝对路径")
    else:
        found.append(
            WindowsCrashDumpLocation(
                kind=WindowsCrashDumpKind.KERNEL_SMALL,
                path=mini_path,
                direct_file=False,
                source=f"CrashControl MinidumpDir（当前模式：{mode_label}）",
                configured_for=("系统小型内存转储",),
                requires_elevation=True,
            )
        )
    return tuple(found), tuple(warnings)


def _discover_local_dump_locations(
    env: Mapping[str, str],
) -> tuple[tuple[WindowsCrashDumpLocation, ...], tuple[str, ...]]:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _LOCAL_DUMPS_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return (), ()
    except OSError:
        return (), ("无法读取 WER LocalDumps 配置；不猜测用户模式转储目录",)

    warnings: list[str] = []
    configs: list[tuple[str, str | None, bool]] = []
    try:
        global_folder, global_ok = _optional_registry_string(key, "DumpFolder")
        global_enabled = _local_dump_settings_present(key)
        if global_ok and global_enabled:
            configs.append(("全局 LocalDumps", global_folder, True))
        elif not global_ok:
            warnings.append("全局 LocalDumps DumpFolder 类型异常；不使用该继承配置")

        index = 0
        while True:
            try:
                app_name = winreg.EnumKey(key, index)
            except OSError as error:
                if getattr(error, "winerror", None) == 259:
                    break
                warnings.append("枚举 LocalDumps 应用配置时失败；保留已确认的配置")
                break
            index += 1
            try:
                app_key = winreg.OpenKey(
                    key,
                    app_name,
                    0,
                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                )
            except OSError:
                warnings.append(f"无法读取 LocalDumps\\{app_name}；跳过该应用配置")
                continue
            try:
                app_folder, app_ok = _optional_registry_string(app_key, "DumpFolder")
                app_enabled = _local_dump_settings_present(app_key)
            finally:
                winreg.CloseKey(app_key)
            if not app_enabled:
                continue
            if not app_ok:
                warnings.append(f"LocalDumps\\{app_name} DumpFolder 类型异常；跳过该应用配置")
                continue
            effective = app_folder if app_folder is not None else global_folder
            inherited_ok = app_folder is not None or global_ok
            configs.append((app_name, effective, inherited_ok))
    finally:
        winreg.CloseKey(key)

    grouped: dict[str, tuple[Path, list[str]]] = {}
    for label, raw_folder, inherited_ok in configs:
        if not inherited_ok:
            continue
        resolved = _resolve_local_dump_path(raw_folder, env)
        if resolved is None:
            warnings.append(
                f"{label} 的 DumpFolder 不是受支持的字面本地路径或默认 %LOCALAPPDATA%\\CrashDumps"
            )
            continue
        normalized = _normalized(resolved)
        current = grouped.get(normalized)
        if current is None:
            grouped[normalized] = (resolved, [label])
        elif label not in current[1]:
            current[1].append(label)

    locations = tuple(
        WindowsCrashDumpLocation(
            kind=WindowsCrashDumpKind.USER_MODE,
            path=path,
            direct_file=False,
            source="WER LocalDumps DumpFolder",
            configured_for=tuple(labels),
            requires_elevation=False,
        )
        for path, labels in grouped.values()
    )
    return locations, tuple(warnings)


def _local_dump_settings_present(key: winreg.HKEYType) -> bool:
    for name in ("DumpFolder", "DumpCount", "DumpType", "CustomDumpFlags"):
        try:
            winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _optional_registry_string(
    key: winreg.HKEYType,
    value_name: str,
) -> tuple[str | None, bool]:
    try:
        value, value_type = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
        return None, False
    if not isinstance(value, str) or not value.strip():
        return None, False
    return value.strip(), True


def _optional_registry_dword(
    key: winreg.HKEYType,
    value_name: str,
) -> tuple[int | None, bool]:
    try:
        value, value_type = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    if value_type != winreg.REG_DWORD or not isinstance(value, int):
        return None, False
    return int(value), True


def _resolve_crash_control_path(raw: str, env: Mapping[str, str]) -> Path | None:
    expanded = _expand_allowed_percent_vars(raw, env, {"systemroot", "windir", "systemdrive"})
    if expanded is None:
        return None
    return _ordinary_absolute_local_path(expanded)


def _resolve_local_dump_path(raw: str | None, env: Mapping[str, str]) -> Path | None:
    if raw is None or _windows_text(raw) == _windows_text(_DEFAULT_LOCAL_DUMP_DIR):
        localappdata = env.get("localappdata")
        if not localappdata:
            return None
        return _ordinary_absolute_local_path(str(Path(localappdata) / "CrashDumps"))
    if "%" in raw:
        return None
    return _ordinary_absolute_local_path(raw)


def _expand_allowed_percent_vars(
    raw: str,
    env: Mapping[str, str],
    allowed: set[str],
) -> str | None:
    text = raw.strip().strip('"')
    pieces: list[str] = []
    index = 0
    while index < len(text):
        marker = text.find("%", index)
        if marker < 0:
            pieces.append(text[index:])
            break
        pieces.append(text[index:marker])
        closing = text.find("%", marker + 1)
        if closing < 0:
            return None
        name = text[marker + 1 : closing].casefold()
        if not name or name not in allowed or name not in env:
            return None
        pieces.append(env[name])
        index = closing + 1
    return "".join(pieces)


def _ordinary_absolute_local_path(value: str) -> Path | None:
    text = value.strip().strip('"')
    if not text or "\x00" in text:
        return None
    raw_path = Path(text)
    rendered_raw = os.fspath(raw_path)
    if rendered_raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
        return None
    if not os.path.isabs(rendered_raw):
        return None
    return _absolute(raw_path)


def _crash_mode_label(mode: int | None, filter_pages: int | None) -> str:
    if mode == 0:
        return "disabled"
    if mode == 1 and filter_pages == 1:
        return "active"
    return {
        1: "complete",
        2: "kernel",
        3: "small",
        7: "automatic",
    }.get(mode, "unknown" if mode is not None else "default/unspecified")


def _root_boundary(path: Path) -> ExactRootBoundary:
    root = _absolute(path)
    metadata = read_file_metadata(root)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError("崩溃转储根没有可验证的普通目录身份")
    if not is_local_fixed_path(root):
        raise RuntimeError("崩溃转储根不在本地固定磁盘")
    return ExactRootBoundary(
        path=root,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
    )


def _file_snapshot(path: Path) -> ExactFileSnapshot:
    candidate = _absolute(path)
    metadata = read_file_metadata(candidate)
    if (
        metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
        or metadata.link_count is None
        or metadata.attributes is None
        or metadata.creation_time_ns is None
        or metadata.last_write_time_ns is None
    ):
        raise RuntimeError("崩溃转储没有可验证的普通文件身份")
    if metadata.link_count != 1:
        raise RuntimeError("拒绝删除硬链接形式的崩溃转储")
    if not is_local_fixed_path(candidate):
        raise RuntimeError("崩溃转储不在本地固定磁盘")
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


def _require_same_entry(
    expected: WindowsCrashDumpEntry,
    current: WindowsCrashDumpEntry,
) -> None:
    if (
        current.kind != expected.kind
        or _normalized(current.path) != _normalized(expected.path)
        or _normalized(current.root) != _normalized(expected.root)
        or current.source != expected.source
        or current.configured_for != expected.configured_for
        or current.requires_elevation != expected.requires_elevation
        or current.root_boundary != expected.root_boundary
        or current.snapshot != expected.snapshot
    ):
        raise RuntimeError("崩溃转储的配置绑定或文件身份在执行前发生变化；请重新检查")


def _is_process_elevated() -> bool:
    if not _WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items() if value}


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _windows_text(value: str) -> str:
    return value.strip().strip('"').replace("/", "\\").casefold()


__all__ = [
    "WindowsCrashDumpDeleteResult",
    "WindowsCrashDumpEntry",
    "WindowsCrashDumpInventory",
    "WindowsCrashDumpKind",
    "WindowsCrashDumpLocation",
    "delete_windows_crash_dump",
    "inventory_windows_crash_dumps",
]
