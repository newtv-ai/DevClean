"""Windows component-store inventory and user-reviewed vendor cleanup."""

# ruff: noqa: RUF001

from __future__ import annotations

import ctypes
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_ANALYZE_TIMEOUT_SECONDS = 15 * 60
_CLEANUP_TIMEOUT_SECONDS = 2 * 60 * 60
_WINDOWS = os.name == "nt"


@dataclass(frozen=True, slots=True)
class DismExecutableIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    last_write_time_ns: int | None


@dataclass(frozen=True, slots=True)
class ComponentStoreReport:
    tool_identity: DismExecutableIdentity
    dism_version: str
    image_version: str
    actual_size_bytes: int | None
    reclaimable_packages: int | None
    cleanup_recommended: bool
    raw_output: str


@dataclass(frozen=True, slots=True)
class ComponentStoreInventory:
    elevated: bool
    report: ComponentStoreReport | None
    cleanup_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ComponentStoreCleanupResult:
    before: ComponentStoreReport
    after: ComponentStoreReport
    cleanup_stdout: str

    @property
    def reported_size_delta_bytes(self) -> int | None:
        """Return only the DISM-reported store-size delta, not physical reclaim."""

        if self.before.actual_size_bytes is None or self.after.actual_size_bytes is None:
            return None
        return max(0, self.before.actual_size_bytes - self.after.actual_size_bytes)


def inventory_windows_component_store(
    environment: Mapping[str, str] | None = None,
) -> ComponentStoreInventory:
    """Read the online Windows component-store report without mutation."""

    if not _WINDOWS:
        raise RuntimeError("Windows 组件存储维护仅支持 Windows")

    executable = dism_executable(environment)
    identity = _dism_identity(executable)
    if not is_process_elevated():
        return ComponentStoreInventory(
            elevated=False,
            report=None,
            cleanup_supported=False,
            reason="DISM 组件存储分析需要管理员权限；DevClean 不会自动提升权限",
        )

    report = _analyze_component_store(identity, environment)
    supported = report.cleanup_recommended
    reason = (
        "DISM 建议清理组件存储；手动 StartComponentCleanup 会跳过自动维护的 30 天宽限期"
        if supported
        else "DISM 当前不建议清理组件存储"
    )
    return ComponentStoreInventory(
        elevated=True,
        report=report,
        cleanup_supported=supported,
        reason=reason,
    )


def cleanup_windows_component_store(
    expected: ComponentStoreReport,
    environment: Mapping[str, str] | None = None,
) -> ComponentStoreCleanupResult:
    """Run exact manual StartComponentCleanup after fresh USER_REVIEW gates."""

    if not _WINDOWS:
        raise RuntimeError("Windows 组件存储维护仅支持 Windows")
    if not is_process_elevated():
        raise PermissionError("需要以管理员身份运行 DevClean；不会自动提升权限")
    if not expected.cleanup_recommended:
        raise ValueError("用户确认的 DISM 报告没有建议组件存储清理")
    if dism_activity_running(environment):
        raise RuntimeError("检测到现有 DISM/DismHost 活动；请等待系统维护完成后再试")

    executable = dism_executable(environment)
    current_identity = _dism_identity(executable)
    if current_identity != expected.tool_identity:
        raise RuntimeError("DISM 可执行文件身份在清理前发生变化；拒绝继续")

    # One fresh vendor analysis immediately before mutation binds the user's
    # reviewed report to the current online image without running two expensive
    # analyses back-to-back inside the same cleanup call.
    current = _analyze_component_store(current_identity, environment)
    if (
        current.tool_identity != expected.tool_identity
        or current.dism_version != expected.dism_version
        or current.image_version != expected.image_version
    ):
        raise RuntimeError("DISM/Windows 映像身份在清理前发生变化；请重新检查")
    if not current.cleanup_recommended:
        raise RuntimeError("组件存储状态已变化，DISM 已不再建议清理")

    command = [
        str(current.tool_identity.path),
        "/Online",
        "/English",
        "/Cleanup-Image",
        "/StartComponentCleanup",
        "/NoRestart",
    ]
    result = _run_command(
        command,
        environment,
        timeout=_CLEANUP_TIMEOUT_SECONDS,
        operation="DISM StartComponentCleanup",
    )

    after_identity = _dism_identity(executable)
    if after_identity != current.tool_identity:
        raise RuntimeError("DISM 可执行文件身份在清理后发生变化；无法确认后置状态")
    after = _analyze_component_store(after_identity, environment)
    if (
        after.dism_version != current.dism_version
        or after.image_version != current.image_version
    ):
        raise RuntimeError("清理后 DISM/Windows 映像身份发生变化；无法确认后置状态")

    return ComponentStoreCleanupResult(
        before=current,
        after=after,
        cleanup_stdout=result.stdout.strip(),
    )


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


def is_process_elevated() -> bool:
    """Return current-process elevation without requesting elevation."""

    if not _WINDOWS:
        return False
    try:
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def dism_activity_running(environment: Mapping[str, str] | None = None) -> bool:
    """Fail closed if another DISM/DismHost process is visible or state is unknown."""

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
    return '"dism.exe"' in output or '"dismhost.exe"' in output


def _analyze_component_store(
    identity: DismExecutableIdentity,
    environment: Mapping[str, str] | None,
) -> ComponentStoreReport:
    command = [
        str(identity.path),
        "/Online",
        "/English",
        "/Cleanup-Image",
        "/AnalyzeComponentStore",
    ]
    result = _run_command(
        command,
        environment,
        timeout=_ANALYZE_TIMEOUT_SECONDS,
        operation="DISM AnalyzeComponentStore",
    )
    return parse_component_store_report(result.stdout, identity)


def parse_component_store_report(
    output: str,
    identity: DismExecutableIdentity,
) -> ComponentStoreReport:
    """Parse the small advisory subset needed to gate the fixed vendor action."""

    dism_version = _required_match(output, r"(?m)^Version\s*:\s*([^\r\n]+)\s*$", "DISM version")
    image_version = _required_match(
        output,
        r"(?m)^Image Version\s*:\s*([^\r\n]+)\s*$",
        "image version",
    )
    recommendation_text = _required_match(
        output,
        r"(?mi)^Component Store Cleanup Recommended\s*:\s*(Yes|No)\s*$",
        "cleanup recommendation",
    ).casefold()

    actual_size_text = _optional_match(
        output,
        r"(?mi)^Actual Size of Component Store\s*:\s*([^\r\n]+)\s*$",
    )
    package_text = _optional_match(
        output,
        r"(?mi)^Number of Reclaimable Packages\s*:\s*(\d+)\s*$",
    )
    actual_size = _parse_size(actual_size_text) if actual_size_text is not None else None
    reclaimable_packages = int(package_text) if package_text is not None else None

    return ComponentStoreReport(
        tool_identity=identity,
        dism_version=dism_version.strip(),
        image_version=image_version.strip(),
        actual_size_bytes=actual_size,
        reclaimable_packages=reclaimable_packages,
        cleanup_recommended=recommendation_text == "yes",
        raw_output=output.strip(),
    )


def _dism_identity(path: Path) -> DismExecutableIdentity:
    candidate = Path(os.path.abspath(path))
    try:
        metadata = read_file_metadata(candidate)
    except OSError as error:
        raise RuntimeError(f"无法读取 DISM 可执行文件身份: {candidate}") from error
    if metadata.is_directory or metadata.is_reparse_point:
        raise RuntimeError(f"DISM 路径不是普通可执行文件: {candidate}")
    if (
        metadata.volume_serial is None
        or metadata.file_id is None
        or metadata.file_id_kind is None
    ):
        raise RuntimeError("DISM 可执行文件没有稳定文件身份")
    if not is_local_fixed_path(candidate):
        raise RuntimeError("DISM 可执行文件不在本地固定磁盘")
    return DismExecutableIdentity(
        path=candidate,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        last_write_time_ns=metadata.last_write_time_ns,
    )


def _run_command(
    command: list[str],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_merged_environment(environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 {operation}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{operation} 失败 (exit {result.returncode}): {detail}")
    return result


def _required_match(output: str, pattern: str, label: str) -> str:
    matches = re.findall(pattern, output)
    if len(matches) != 1:
        raise RuntimeError(f"DISM {label} 输出缺失或不唯一")
    return str(matches[0])


def _optional_match(output: str, pattern: str) -> str | None:
    matches = re.findall(pattern, output)
    if len(matches) > 1:
        raise RuntimeError("DISM report field is ambiguous")
    if not matches:
        return None
    return str(matches[0]).strip()


def _parse_size(text: str) -> int | None:
    match = re.fullmatch(
        r"\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(bytes?|KB|MB|GB|TB)\s*",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    unit = match.group(2).casefold()
    multiplier = {
        "byte": 1,
        "bytes": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
    }[unit]
    return int(number * multiplier)


def _merged_casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    return {key.casefold(): value for key, value in _merged_environment(environment).items()}


def _merged_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    return env


__all__ = [
    "ComponentStoreCleanupResult",
    "ComponentStoreInventory",
    "ComponentStoreReport",
    "DismExecutableIdentity",
    "cleanup_windows_component_store",
    "dism_activity_running",
    "dism_executable",
    "inventory_windows_component_store",
    "is_process_elevated",
    "parse_component_store_report",
]
