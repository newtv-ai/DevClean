"""Exact Windows Delivery Optimization cache inventory and per-file vendor removal."""

# ruff: noqa: RUF001

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_WINDOWS = os.name == "nt"
_SAFE_STATUS = "caching"
_ACTIVE_STATUSES = frozenset({"downloading", "complete", "paused"})

_STATUS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module -LiteralPath $env:DEVCLEAN_DO_MODULE -Force -ErrorAction Stop
$items = @(
  Get-DeliveryOptimizationStatus -AsObject -ErrorAction Stop | ForEach-Object {
    $expire = $null
    if ($null -ne $_.ExpireOn) {
      try {
        $expire = ([DateTime]$_.ExpireOn).ToUniversalTime().ToString(
          'o', [Globalization.CultureInfo]::InvariantCulture
        )
      } catch {
        $expire = [string]$_.ExpireOn
      }
    }
    [pscustomobject]@{
      FileId = [string]$_.FileId
      FileSize = [Int64]$_.FileSize
      FileSizeInCache = [Int64]$_.FileSizeInCache
      Status = [string]$_.Status
      Priority = [string]$_.Priority
      ExpireOn = $expire
      IsPinned = [bool]$_.IsPinned
      Caller = [string]$_.PredefinedCallerApplication
    }
  }
)
[pscustomobject]@{ Items = $items } | ConvertTo-Json -Depth 4 -Compress
"""

_DELETE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module -LiteralPath $env:DEVCLEAN_DO_MODULE -Force -ErrorAction Stop
$fileId = [string]$env:DEVCLEAN_DO_FILE_ID
if ([string]::IsNullOrWhiteSpace($fileId)) { throw 'missing file id' }
Delete-DeliveryOptimizationCache -FileID $fileId -Force -ErrorAction Stop
"""


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    creation_time_ns: int | None
    last_write_time_ns: int | None


@dataclass(frozen=True, slots=True)
class DeliveryOptimizationEntry:
    file_id: str
    file_size: int
    cache_bytes: int
    status: str
    priority: str
    expire_on: datetime | None
    pinned: bool
    caller: str
    decision_class: str
    deletion_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DeliveryOptimizationInventory:
    elevated: bool
    powershell: WindowsFileIdentity
    module_manifest: WindowsFileIdentity
    entries: tuple[DeliveryOptimizationEntry, ...]

    @property
    def cache_bytes(self) -> int:
        return sum(entry.cache_bytes for entry in self.entries)


@dataclass(frozen=True, slots=True)
class DeliveryOptimizationDeleteResult:
    entry: DeliveryOptimizationEntry
    before_cache_bytes: int
    after_cache_bytes: int
    stdout: str

    @property
    def observed_cache_delta(self) -> int:
        return max(0, self.before_cache_bytes - self.after_cache_bytes)


def inventory_delivery_optimization_cache(
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> DeliveryOptimizationInventory:
    """Read exact Delivery Optimization jobs through the built-in Windows module."""

    if not _WINDOWS and not _test_windows_override(environment):
        raise RuntimeError("Delivery Optimization 缓存维护仅支持 Windows")
    powershell = _windows_powershell(environment)
    module = _delivery_optimization_module(environment)
    powershell_before = _file_identity(powershell, "Windows PowerShell")
    module_before = _file_identity(module, "DeliveryOptimization module")
    raw = _run_powershell(
        powershell,
        module,
        _STATUS_SCRIPT,
        environment,
        timeout=120,
    )
    powershell_after = _file_identity(powershell, "Windows PowerShell")
    module_after = _file_identity(module, "DeliveryOptimization module")
    if powershell_before != powershell_after or module_before != module_after:
        raise RuntimeError("PowerShell/DeliveryOptimization module 身份在检查期间发生变化")

    payload = _json_object(raw.stdout, "Delivery Optimization status")
    items = payload.get("Items", [])
    if items is None:
        items = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise RuntimeError("Delivery Optimization status Items 不是 JSON array")

    elevated = _is_process_elevated()
    current = _as_utc(now or datetime.now(UTC))
    entries: list[DeliveryOptimizationEntry] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Delivery Optimization status 包含非 object 条目")
        entry = _entry_from_payload(item, elevated=elevated, now=current)
        key = entry.file_id.casefold()
        if key in seen:
            raise RuntimeError(f"Delivery Optimization FileId 重复: {entry.file_id}")
        seen.add(key)
        entries.append(entry)
    entries.sort(key=lambda item: item.cache_bytes, reverse=True)
    return DeliveryOptimizationInventory(
        elevated=elevated,
        powershell=powershell_after,
        module_manifest=module_after,
        entries=tuple(entries),
    )


def delete_delivery_optimization_cache_file(
    expected: DeliveryOptimizationEntry,
    expected_inventory: DeliveryOptimizationInventory,
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> DeliveryOptimizationDeleteResult:
    """Delete one exact reviewed unpinned cached file through Microsoft's cmdlet."""

    if not expected.deletion_supported:
        raise ValueError(expected.reason or "该 Delivery Optimization 条目当前不可删除")
    if not _is_process_elevated():
        raise PermissionError("删除 Delivery Optimization 缓存需要管理员进程；DevClean 不会自动提权")

    initial = inventory_delivery_optimization_cache(environment, now=now)
    _require_same_tools(expected_inventory, initial)
    current = _exact_entry(initial.entries, expected.file_id)
    _require_same_entry(expected, current)
    if not current.deletion_supported:
        raise RuntimeError(current.reason)

    fresh = inventory_delivery_optimization_cache(environment, now=now)
    _require_same_tools(initial, fresh)
    current = _exact_entry(fresh.entries, expected.file_id)
    _require_same_entry(expected, current)
    if not current.deletion_supported:
        raise RuntimeError(current.reason)

    env = _merged_environment(environment)
    env["DEVCLEAN_DO_FILE_ID"] = current.file_id
    result = _run_powershell(
        fresh.powershell.path,
        fresh.module_manifest.path,
        _DELETE_SCRIPT,
        env,
        timeout=300,
    )

    after = inventory_delivery_optimization_cache(environment, now=now)
    _require_same_tools(fresh, after)
    if any(item.file_id.casefold() == current.file_id.casefold() for item in after.entries):
        raise RuntimeError("Delivery Optimization 删除命令返回成功，但精确 FileId 仍然存在")
    return DeliveryOptimizationDeleteResult(
        entry=current,
        before_cache_bytes=fresh.cache_bytes,
        after_cache_bytes=after.cache_bytes,
        stdout=(result.stdout or result.stderr).strip(),
    )


def _entry_from_payload(
    payload: dict[str, Any],
    *,
    elevated: bool,
    now: datetime,
) -> DeliveryOptimizationEntry:
    file_id = _required_string(payload, "FileId")
    file_size = _nonnegative_int(payload.get("FileSize"), "FileSize")
    cache_bytes = _nonnegative_int(payload.get("FileSizeInCache"), "FileSizeInCache")
    status = _required_string(payload, "Status")
    status_folded = status.casefold()
    priority = str(payload.get("Priority", "")).strip()
    caller = str(payload.get("Caller", "")).strip()
    pinned = _required_bool(payload.get("IsPinned"), "IsPinned")
    expire_on = _optional_datetime(payload.get("ExpireOn"), "ExpireOn")

    decision_class = "REPORT_ONLY"
    deletion_supported = False
    if pinned:
        reason = "该文件被 Delivery Optimization 明确 pin；DevClean 永远不会使用 IncludePinnedFiles"
    elif status_folded in _ACTIVE_STATUSES:
        reason = f"Delivery Optimization 状态为 {status}；不是稳定的缓存保留态"
    elif status_folded != _SAFE_STATUS:
        reason = f"Delivery Optimization 返回未审计状态 {status!r}；fail closed"
    elif cache_bytes <= 0:
        reason = "该 FileId 当前没有可回收的缓存字节"
    elif expire_on is not None and expire_on <= now:
        decision_class = "DETERMINISTIC_CANDIDATE"
        deletion_supported = elevated
        reason = (
            "未 pin 且已到达 Delivery Optimization 自己的 ExpireOn；"
            "属于 vendor-expired cache"
            if elevated
            else "vendor-expired cache，但当前 DevClean 未以管理员身份运行；只报告"
        )
    else:
        decision_class = "USER_REVIEW"
        deletion_supported = elevated
        reason = (
            "未 pin 的 Caching 文件仍在 vendor 保留期内；删除可重下载但会损失本机/对等缓存价值"
            if elevated
            else "未 pin 的 Caching 文件可由用户决定，但当前 DevClean 未以管理员身份运行；只报告"
        )

    return DeliveryOptimizationEntry(
        file_id=file_id,
        file_size=file_size,
        cache_bytes=cache_bytes,
        status=status,
        priority=priority,
        expire_on=expire_on,
        pinned=pinned,
        caller=caller,
        decision_class=decision_class,
        deletion_supported=deletion_supported,
        reason=reason,
    )


def _require_same_tools(
    expected: DeliveryOptimizationInventory,
    current: DeliveryOptimizationInventory,
) -> None:
    if (
        expected.powershell != current.powershell
        or expected.module_manifest != current.module_manifest
    ):
        raise RuntimeError("Windows PowerShell/DeliveryOptimization module 身份已变化；请重新检查")


def _require_same_entry(
    expected: DeliveryOptimizationEntry,
    current: DeliveryOptimizationEntry,
) -> None:
    if (
        current.file_id.casefold() != expected.file_id.casefold()
        or current.file_size != expected.file_size
        or current.cache_bytes != expected.cache_bytes
        or current.status.casefold() != expected.status.casefold()
        or current.priority != expected.priority
        or current.expire_on != expected.expire_on
        or current.pinned != expected.pinned
        or current.caller != expected.caller
    ):
        raise RuntimeError("Delivery Optimization FileId/status/pin/expiry/cache identity 已变化；请重新检查")


def _exact_entry(
    entries: Sequence[DeliveryOptimizationEntry],
    file_id: str,
) -> DeliveryOptimizationEntry:
    matches = [entry for entry in entries if entry.file_id.casefold() == file_id.casefold()]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一确认 Delivery Optimization FileId {file_id!r}: found={len(matches)}")
    return matches[0]


def _windows_powershell(environment: Mapping[str, str] | None) -> Path:
    env = _casefold_env(environment)
    override = env.get("devclean_windows_powershell")
    if override:
        return Path(override)
    system_root = env.get("systemroot") or r"C:\Windows"
    return Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _delivery_optimization_module(environment: Mapping[str, str] | None) -> Path:
    env = _casefold_env(environment)
    override = env.get("devclean_delivery_optimization_module")
    if override:
        return Path(override)
    system_root = env.get("systemroot") or r"C:\Windows"
    return (
        Path(system_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
        / "DeliveryOptimization"
        / "DeliveryOptimization.psd1"
    )


def _file_identity(path: Path, label: str) -> WindowsFileIdentity:
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        if candidate.is_symlink() or candidate.is_junction():
            raise RuntimeError(f"{label} 不能是 symlink/junction/reparse")
        metadata = read_file_metadata(candidate)
    except OSError as error:
        raise RuntimeError(f"无法读取 {label}: {candidate}") from error
    if metadata.is_directory or metadata.is_reparse_point:
        raise RuntimeError(f"{label} 不是普通文件: {candidate}")
    if not is_local_fixed_path(candidate):
        raise RuntimeError(f"{label} 不在本地固定磁盘: {candidate}")
    if (
        metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError(f"{label} 没有稳定文件身份")
    return WindowsFileIdentity(
        path=candidate,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def _run_powershell(
    powershell: Path,
    module: Path,
    script: str,
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = _merged_environment(environment)
    env["DEVCLEAN_DO_MODULE"] = str(module)
    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Delivery Optimization PowerShell: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Delivery Optimization PowerShell 失败 (exit {result.returncode}): {detail}"
        )
    return result


def _is_process_elevated() -> bool:
    if not _WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _json_object(output: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"无法解析 {label} JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 未返回 JSON object")
    return {str(key): item for key, item in value.items()}


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Delivery Optimization 条目缺少 {key}")
    return value.strip()


def _required_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"Delivery Optimization {label} 不是 boolean")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"Delivery Optimization {label} 不是有效整数") from error
    if parsed < 0:
        raise RuntimeError(f"Delivery Optimization {label} 不能为负数")
    return parsed


def _optional_datetime(value: object, label: str) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"Delivery Optimization {label} 不是 string/null")
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"Delivery Optimization {label} 不是 ISO-8601: {text!r}") from error
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise RuntimeError("Delivery Optimization 时间没有时区信息")
    return value.astimezone(UTC)


def _merged_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    return env


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = _merged_environment(environment)
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _test_windows_override(environment: Mapping[str, str] | None) -> bool:
    if environment is None:
        return False
    return _casefold_env(environment).get("devclean_test_windows") == "1"


__all__ = [
    "DeliveryOptimizationDeleteResult",
    "DeliveryOptimizationEntry",
    "DeliveryOptimizationInventory",
    "WindowsFileIdentity",
    "delete_delivery_optimization_cache_file",
    "inventory_delivery_optimization_cache",
]
