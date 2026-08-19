"""Windows previous-installation inventory and vendor-owned cleanup."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.windows_component_store_maintenance import is_process_elevated
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_WINDOWS = os.name == "nt"
_CLEANUP_TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class WindowsFileIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    last_write_time_ns: int | None


@dataclass(frozen=True, slots=True)
class WindowsDirectoryIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    creation_time_ns: int | None


@dataclass(frozen=True, slots=True)
class PreviousInstallInventory:
    elevated: bool
    system_root: Path
    windows_old: Path
    windows_old_identity: WindowsDirectoryIdentity | None
    windows_old_logical_bytes: int | None
    setup_rollback_root: Path
    setup_rollback_present: bool
    os_uninstall_window_days: int | None
    cleanmgr_identity: WindowsFileIdentity | None
    cleanup_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PreviousInstallCleanupResult:
    before: PreviousInstallInventory
    after: PreviousInstallInventory
    command: tuple[str, ...]

    @property
    def windows_old_removed(self) -> bool:
        return self.after.windows_old_identity is None


def inventory_previous_windows_installation(
    environment: Mapping[str, str] | None = None,
) -> PreviousInstallInventory:
    """Inventory the exact current Windows previous-installation state read-only."""

    if not _WINDOWS:
        raise RuntimeError("以前的 Windows 安装维护仅支持 Windows")

    env = _merged_casefold_env(environment)
    system_root = Path(env.get("systemroot") or r"C:\Windows")
    system_root = _ordinary_local_system_root(system_root)
    volume_root = Path(system_root.anchor)
    windows_old = volume_root / "Windows.old"
    rollback_root = volume_root / "$WINDOWS.~BT"

    elevated = is_process_elevated()
    old_identity: WindowsDirectoryIdentity | None = None
    old_bytes: int | None = None
    if windows_old.exists():
        old_identity = _directory_identity(windows_old, "Windows.old")
        old_bytes = _directory_bytes_best_effort(windows_old)

    rollback_present = _ordinary_directory_exists(rollback_root)
    uninstall_window = _get_os_uninstall_window(environment) if elevated else None

    cleanmgr_identity: WindowsFileIdentity | None = None
    try:
        cleanmgr_identity = _file_identity(cleanmgr_executable(environment), "cleanmgr.exe")
    except RuntimeError:
        cleanmgr_identity = None

    if old_identity is None:
        supported = False
        reason = "当前系统盘没有可验证的 Windows.old；没有以前的 Windows 安装可交给 Windows 清理"
    elif not elevated:
        supported = False
        reason = "删除以前的 Windows 安装需要管理员权限；DevClean 不会自动提升权限"
    elif cleanmgr_identity is None:
        supported = False
        reason = "无法验证本机 Windows cleanmgr.exe；拒绝授予升级遗留清理权限"
    else:
        supported = True
        reason = (
            "Windows.old 是以前的 Windows 安装/回滚数据；删除后无法恢复该副本，"
            "且其中可能仍有可手工取回的个人文件"
        )

    return PreviousInstallInventory(
        elevated=elevated,
        system_root=system_root,
        windows_old=windows_old,
        windows_old_identity=old_identity,
        windows_old_logical_bytes=old_bytes,
        setup_rollback_root=rollback_root,
        setup_rollback_present=rollback_present,
        os_uninstall_window_days=uninstall_window,
        cleanmgr_identity=cleanmgr_identity,
        cleanup_supported=supported,
        reason=reason,
    )


def cleanup_previous_windows_installation(
    expected: PreviousInstallInventory,
    environment: Mapping[str, str] | None = None,
) -> PreviousInstallCleanupResult:
    """Run Windows' documented upgrade-leftover cleanup after fresh USER_REVIEW gates."""

    if not _WINDOWS:
        raise RuntimeError("以前的 Windows 安装维护仅支持 Windows")
    if not is_process_elevated():
        raise PermissionError("需要以管理员身份运行 DevClean；不会自动提升权限")
    if not expected.cleanup_supported or expected.windows_old_identity is None:
        raise ValueError("用户确认的检查结果没有可执行的以前 Windows 安装清理")
    if windows_setup_or_cleanup_activity_running(environment):
        raise RuntimeError("检测到 Windows 安装/升级/磁盘清理活动；请等待完成后再试")

    current = inventory_previous_windows_installation(environment)
    if not current.cleanup_supported or current.windows_old_identity is None:
        raise RuntimeError("以前的 Windows 安装状态已变化；请重新检查")
    if (
        current.system_root != expected.system_root
        or current.windows_old != expected.windows_old
        or current.windows_old_identity != expected.windows_old_identity
        or current.cleanmgr_identity != expected.cleanmgr_identity
    ):
        raise RuntimeError("Windows.old 或 Windows 清理工具身份已变化；拒绝继续")

    cleanmgr = current.cleanmgr_identity
    assert cleanmgr is not None
    command = (str(cleanmgr.path), "/AUTOCLEAN")
    _run_cleanup(command, environment)

    after = inventory_previous_windows_installation(environment)
    if after.system_root != current.system_root:
        raise RuntimeError("清理后 Windows 系统根目录身份发生变化；无法确认结果")
    if after.windows_old_identity is not None:
        raise RuntimeError("Windows 清理已返回，但 Windows.old 仍存在；不报告清理成功")

    return PreviousInstallCleanupResult(
        before=current,
        after=after,
        command=command,
    )


def cleanmgr_executable(environment: Mapping[str, str] | None = None) -> Path:
    env = _merged_casefold_env(environment)
    override = env.get("devclean_cleanmgr_exe")
    if override:
        return Path(override)
    system_root = env.get("systemroot") or r"C:\Windows"
    return Path(system_root) / "System32" / "cleanmgr.exe"


def dism_executable(environment: Mapping[str, str] | None = None) -> Path:
    env = _merged_casefold_env(environment)
    override = env.get("devclean_dism_exe")
    if override:
        return Path(override)
    system_root = env.get("systemroot") or r"C:\Windows"
    return Path(system_root) / "System32" / "dism.exe"


def tasklist_executable(environment: Mapping[str, str] | None = None) -> Path:
    env = _merged_casefold_env(environment)
    override = env.get("devclean_tasklist_exe")
    if override:
        return Path(override)
    system_root = env.get("systemroot") or r"C:\Windows"
    return Path(system_root) / "System32" / "tasklist.exe"


def windows_setup_or_cleanup_activity_running(
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Fail closed if setup/cleanup activity is visible or process state is unknown."""

    if not _WINDOWS:
        return True
    command = [str(tasklist_executable(environment)), "/FO", "CSV", "/NH"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=_merged_environment(environment),
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    output = result.stdout.casefold()
    blocked = (
        '"cleanmgr.exe"',
        '"dism.exe"',
        '"dismhost.exe"',
        '"setuphost.exe"',
        '"setupprep.exe"',
    )
    return any(name in output for name in blocked)


def _ordinary_local_system_root(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        metadata = read_file_metadata(candidate)
    except OSError as error:
        raise RuntimeError(f"无法验证 Windows 系统根目录: {candidate}") from error
    if not metadata.is_directory or metadata.is_reparse_point:
        raise RuntimeError("Windows 系统根目录不是可验证的普通目录")
    if not is_local_fixed_path(candidate):
        raise RuntimeError("Windows 系统根目录不在本地固定磁盘")
    return candidate


def _ordinary_directory_exists(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        metadata = read_file_metadata(path)
    except OSError:
        return False
    return metadata.is_directory and not metadata.is_reparse_point


def _directory_identity(path: Path, label: str) -> WindowsDirectoryIdentity:
    candidate = Path(os.path.abspath(path))
    try:
        metadata = read_file_metadata(candidate)
    except OSError as error:
        raise RuntimeError(f"无法读取 {label} 身份: {candidate}") from error
    if not metadata.is_directory or metadata.is_reparse_point:
        raise RuntimeError(f"{label} 不是普通目录或存在重定向/reparse")
    if not is_local_fixed_path(candidate):
        raise RuntimeError(f"{label} 不在本地固定磁盘")
    if metadata.volume_serial is None or metadata.file_id is None or metadata.file_id_kind is None:
        raise RuntimeError(f"{label} 没有稳定文件身份")
    return WindowsDirectoryIdentity(
        path=candidate,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        creation_time_ns=metadata.creation_time_ns,
    )


def _file_identity(path: Path, label: str) -> WindowsFileIdentity:
    candidate = Path(os.path.abspath(path))
    try:
        metadata = read_file_metadata(candidate)
    except OSError as error:
        raise RuntimeError(f"无法读取 {label} 身份: {candidate}") from error
    if metadata.is_directory or metadata.is_reparse_point:
        raise RuntimeError(f"{label} 不是普通文件")
    if not is_local_fixed_path(candidate):
        raise RuntimeError(f"{label} 不在本地固定磁盘")
    if metadata.volume_serial is None or metadata.file_id is None or metadata.file_id_kind is None:
        raise RuntimeError(f"{label} 没有稳定文件身份")
    return WindowsFileIdentity(
        path=candidate,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def _get_os_uninstall_window(environment: Mapping[str, str] | None) -> int | None:
    path = dism_executable(environment)
    try:
        _file_identity(path, "dism.exe")
        result = subprocess.run(
            [str(path), "/Online", "/English", "/Get-OSUninstallWindow"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env=_merged_environment(environment),
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return None
    if result.returncode != 0:
        return None
    matches = re.findall(r"(?mi)^Uninstall Window\s*:\s*(\d+)\s*$", result.stdout)
    if len(matches) != 1:
        return None
    return int(matches[0])


def _directory_bytes_best_effort(root: Path) -> int | None:
    total = 0
    saw_entry = False
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            # Do not follow directory links/junctions during the informational size walk.
            kept: list[str] = []
            for name in directories:
                child = Path(current) / name
                try:
                    metadata = read_file_metadata(child)
                except OSError:
                    continue
                if metadata.is_directory and not metadata.is_reparse_point:
                    kept.append(name)
            directories[:] = kept
            for name in files:
                child = Path(current) / name
                try:
                    if child.is_symlink():
                        continue
                    total += child.stat().st_size
                    saw_entry = True
                except OSError:
                    continue
    except OSError:
        return None
    return total if saw_entry else 0


def _run_cleanup(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> None:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=_CLEANUP_TIMEOUT_SECONDS,
            env=_merged_environment(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Windows cleanmgr /AUTOCLEAN: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Windows cleanmgr /AUTOCLEAN 失败 (exit {result.returncode}): {detail}")


def _merged_casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    return {key.casefold(): value for key, value in _merged_environment(environment).items()}


def _merged_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    return env


__all__ = [
    "PreviousInstallCleanupResult",
    "PreviousInstallInventory",
    "WindowsDirectoryIdentity",
    "WindowsFileIdentity",
    "cleanmgr_executable",
    "cleanup_previous_windows_installation",
    "inventory_previous_windows_installation",
    "windows_setup_or_cleanup_activity_running",
]
