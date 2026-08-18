"""Android SDK installed-package inventory and vendor-owned uninstall actions."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.core.android_sdk_cleanup import (
    android_sdk_process_running,
    android_sdk_roots,
    clear_android_sdk_process_cache,
)
from devclean.platform.windows.volumes import is_local_fixed_path

_PACKAGE_ID = re.compile(r"^[A-Za-z0-9._+\-]+(?:;[A-Za-z0-9._+\-]+)*$")


@dataclass(frozen=True, slots=True)
class AndroidSdkPackageEntry:
    sdk_root: Path
    package_id: str
    version: str
    description: str
    location: str
    installed_path: Path | None
    logical_bytes: int
    deletion_supported: bool
    protected_reason: str = ""


@dataclass(frozen=True, slots=True)
class AndroidSdkRootInventory:
    sdk_root: Path
    sdkmanager: Path | None
    local_fixed: bool
    packages: tuple[AndroidSdkPackageEntry, ...]
    error: str = ""

    @property
    def package_bytes(self) -> int:
        return sum(package.logical_bytes for package in self.packages)


@dataclass(frozen=True, slots=True)
class AndroidSdkInventory:
    roots: tuple[AndroidSdkRootInventory, ...]

    @property
    def package_bytes(self) -> int:
        return sum(root.package_bytes for root in self.roots)


@dataclass(frozen=True, slots=True)
class AndroidSdkUninstallResult:
    sdk_root: Path
    package_id: str
    version: str
    installed_path: Path | None
    before_bytes: int
    after_bytes: int
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_android_sdk_packages(
    environment: Mapping[str, str] | None = None,
) -> AndroidSdkInventory:
    """List installed packages from every source-backed Android SDK root."""

    roots = android_sdk_roots(environment).sdk_roots
    inventories: list[AndroidSdkRootInventory] = []
    for raw_root in roots:
        root = Path(str(raw_root))
        try:
            exists = root.is_dir()
        except OSError:
            exists = False
        if not exists:
            continue
        local_fixed = is_local_fixed_path(root)
        sdkmanager = _find_sdkmanager(root)
        if sdkmanager is None:
            inventories.append(
                AndroidSdkRootInventory(
                    sdk_root=root,
                    sdkmanager=None,
                    local_fixed=local_fixed,
                    packages=(),
                    error="没有找到这个 SDK 自己的 sdkmanager；只报告根目录，不提供卸载",
                )
            )
            continue
        try:
            result = _run_sdkmanager(
                sdkmanager,
                ("--list", f"--sdk_root={root}"),
                environment,
                timeout=240,
            )
            packages = _parse_installed_packages(result.stdout, root, local_fixed)
        except Exception as error:
            inventories.append(
                AndroidSdkRootInventory(
                    sdk_root=root,
                    sdkmanager=sdkmanager,
                    local_fixed=local_fixed,
                    packages=(),
                    error=str(error),
                )
            )
            continue
        inventories.append(
            AndroidSdkRootInventory(
                sdk_root=root,
                sdkmanager=sdkmanager,
                local_fixed=local_fixed,
                packages=packages,
            )
        )
    return AndroidSdkInventory(tuple(inventories))


def uninstall_android_sdk_package(
    sdk_root: Path,
    package_id: str,
    *,
    expected_version: str,
    expected_location: str,
    environment: Mapping[str, str] | None = None,
) -> AndroidSdkUninstallResult:
    """Uninstall one exact installed package through sdkmanager."""

    if not _PACKAGE_ID.fullmatch(package_id):
        raise ValueError(f"Android SDK package id 无效: {package_id}")
    inventory = _inventory_exact_root(sdk_root, environment)
    if not inventory.local_fixed:
        raise ValueError(
            "Android SDK 不在本地固定磁盘上；共享、远程、可移动或 reparse "
            "重定向的 SDK 只允许检查"
        )
    if inventory.sdkmanager is None:
        raise RuntimeError("当前 Android SDK 没有可用的 sdkmanager")
    selected = next(
        (package for package in inventory.packages if package.package_id == package_id),
        None,
    )
    if selected is None:
        raise FileNotFoundError(f"Android SDK package 已不存在: {package_id}")
    if selected.version != expected_version or selected.location != expected_location:
        raise ValueError(
            f"Android SDK package {package_id} 在选择后已变化；请重新统计后再卸载"
        )
    if not selected.deletion_supported:
        reason = selected.protected_reason or "该 package 当前没有安全卸载权限"
        raise ValueError(f"拒绝卸载 {package_id}: {reason}")

    clear_android_sdk_process_cache()
    if android_sdk_process_running() or _android_runtime_process_running():
        raise RuntimeError(
            "Android Studio、Gradle、sdkmanager、ADB 或 Emulator 正在使用 SDK；"
            "请关闭相关工具后再卸载 package"
        )

    # Fresh vendor listing immediately before mutation closes the stale-selection
    # window. The root and package identity are then pinned on the sdkmanager
    # command line; DevClean never converts the package's Location column into a
    # recursive-delete capability.
    inventory = _inventory_exact_root(sdk_root, environment)
    selected = next(
        (package for package in inventory.packages if package.package_id == package_id),
        None,
    )
    if selected is None:
        raise FileNotFoundError(f"Android SDK package 已不存在: {package_id}")
    if selected.version != expected_version or selected.location != expected_location:
        raise ValueError(
            f"Android SDK package {package_id} 在执行前已变化；请重新统计后再卸载"
        )
    if not selected.deletion_supported or inventory.sdkmanager is None:
        raise ValueError(f"Android SDK package {package_id} 已失去安全卸载权限")

    before = selected.logical_bytes
    result = _run_sdkmanager(
        inventory.sdkmanager,
        ("--uninstall", package_id, f"--sdk_root={inventory.sdk_root}"),
        environment,
        timeout=1800,
    )

    after_inventory = _inventory_exact_root(sdk_root, environment)
    if any(package.package_id == package_id for package in after_inventory.packages):
        raise RuntimeError(
            f"sdkmanager 返回成功，但 package 仍被列为已安装: {package_id}"
        )
    after = _path_bytes(selected.installed_path)
    return AndroidSdkUninstallResult(
        sdk_root=inventory.sdk_root,
        package_id=package_id,
        version=expected_version,
        installed_path=selected.installed_path,
        before_bytes=before,
        after_bytes=after,
        output=(result.stdout or result.stderr).strip(),
    )


def _inventory_exact_root(
    sdk_root: Path,
    environment: Mapping[str, str] | None,
) -> AndroidSdkRootInventory:
    root = Path(os.path.abspath(os.fspath(sdk_root.expanduser())))
    source_roots = android_sdk_roots(environment).sdk_roots
    if not any(_normalized(root) == _normalized(Path(str(item))) for item in source_roots):
        raise ValueError(f"不是当前来源可确认的 Android SDK 根: {root}")
    if not root.is_dir():
        raise FileNotFoundError(f"Android SDK 根不存在: {root}")
    sdkmanager = _find_sdkmanager(root)
    if sdkmanager is None:
        return AndroidSdkRootInventory(
            sdk_root=root,
            sdkmanager=None,
            local_fixed=is_local_fixed_path(root),
            packages=(),
            error="没有找到 sdkmanager",
        )
    result = _run_sdkmanager(
        sdkmanager,
        ("--list", f"--sdk_root={root}"),
        environment,
        timeout=240,
    )
    local_fixed = is_local_fixed_path(root)
    return AndroidSdkRootInventory(
        sdk_root=root,
        sdkmanager=sdkmanager,
        local_fixed=local_fixed,
        packages=_parse_installed_packages(result.stdout, root, local_fixed),
    )


def _find_sdkmanager(sdk_root: Path) -> Path | None:
    names = ("sdkmanager.bat", "sdkmanager")
    candidates: list[Path] = []
    for name in names:
        candidates.append(sdk_root / "cmdline-tools" / "latest" / "bin" / name)
    cmdline_root = sdk_root / "cmdline-tools"
    try:
        children = sorted(
            (
                child
                for child in cmdline_root.iterdir()
                if child.is_dir() and child.name.casefold() != "latest"
            ),
            key=lambda item: item.name.casefold(),
            reverse=True,
        )
    except OSError:
        children = []
    for child in children:
        for name in names:
            candidates.append(child / "bin" / name)
    for name in names:
        candidates.append(sdk_root / "tools" / "bin" / name)

    for candidate in candidates:
        try:
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and _strict_descendant(candidate, sdk_root)
            ):
                return candidate
        except OSError:
            continue
    return None


def _parse_installed_packages(
    output: str,
    sdk_root: Path,
    root_local_fixed: bool,
) -> tuple[AndroidSdkPackageEntry, ...]:
    lines = output.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().casefold() == "installed packages:"
        ),
        None,
    )
    if start is None:
        raise RuntimeError(
            "sdkmanager --list 输出中没有 Installed packages 表；无法安全解析已安装 package"
        )

    entries: list[AndroidSdkPackageEntry] = []
    in_table = False
    saw_row = False
    for line in lines[start + 1 :]:
        stripped = line.strip()
        parts = [part.strip() for part in line.split("|")]
        if not in_table:
            if len(parts) >= 4 and parts[0].casefold() == "path":
                in_table = True
            continue
        if not stripped:
            if saw_row:
                break
            continue
        if set(stripped) <= {"-", " ", "|"}:
            continue
        if len(parts) < 4:
            if saw_row:
                break
            continue
        package_id, version, description, location = parts[:4]
        if not _PACKAGE_ID.fullmatch(package_id):
            if saw_row:
                break
            continue
        saw_row = True
        installed_path = _resolve_package_location(sdk_root, location)
        protected_reason = _protected_package_reason(package_id)
        location_local = (
            installed_path is not None and is_local_fixed_path(installed_path)
        )
        deletion_supported = (
            root_local_fixed
            and location_local
            and not protected_reason
        )
        entries.append(
            AndroidSdkPackageEntry(
                sdk_root=sdk_root,
                package_id=package_id,
                version=version,
                description=description,
                location=location,
                installed_path=installed_path,
                logical_bytes=_path_bytes(installed_path),
                deletion_supported=deletion_supported,
                protected_reason=protected_reason,
            )
        )
    if not in_table:
        raise RuntimeError("sdkmanager Installed packages 表缺少列标题")
    entries.sort(key=lambda package: package.logical_bytes, reverse=True)
    return tuple(entries)


def _resolve_package_location(sdk_root: Path, location: str) -> Path | None:
    if not location:
        return None
    raw = Path(location.replace("\\", os.sep).replace("/", os.sep))
    path = raw if raw.is_absolute() else sdk_root / raw
    path = Path(os.path.abspath(os.fspath(path)))
    if not _strict_descendant(path, sdk_root):
        return None
    return path


def _protected_package_reason(package_id: str) -> str:
    if package_id == "tools" or package_id.startswith("cmdline-tools;"):
        return "这是 sdkmanager 自身所属的命令行工具 package；不能让执行器卸载自己"
    return ""


def _android_runtime_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -ieq 'adb' -or "
        "$_.ProcessName -ieq 'emulator' -or "
        "$_.ProcessName -like 'qemu-system-*' }; "
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


def _run_sdkmanager(
    sdkmanager: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    env.setdefault("LANG", "C")
    env.setdefault("LC_ALL", "C")
    java_options = env.get("JAVA_TOOL_OPTIONS", "").strip()
    locale_options = "-Duser.language=en -Duser.country=US"
    env["JAVA_TOOL_OPTIONS"] = (
        f"{java_options} {locale_options}".strip()
        if java_options
        else locale_options
    )
    command = (str(sdkmanager), *arguments)
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Android sdkmanager: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"sdkmanager 失败 (exit {result.returncode}): {detail}"
        )
    return result


def _path_bytes(path: Path | None) -> int:
    if path is None:
        return 0
    try:
        if path.is_file():
            return path.stat().st_size
        if not path.is_dir():
            return 0
    except OSError:
        return 0
    total = 0
    try:
        for directory, subdirs, files in os.walk(path, followlinks=False):
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
                child = base / name
                try:
                    if child.is_symlink():
                        continue
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _strict_descendant(path: Path, root: Path) -> bool:
    try:
        common = Path(os.path.commonpath((str(root), str(path))))
    except ValueError:
        return False
    return _normalized(common) == _normalized(root) and _normalized(path) != _normalized(root)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "AndroidSdkInventory",
    "AndroidSdkPackageEntry",
    "AndroidSdkRootInventory",
    "AndroidSdkUninstallResult",
    "inventory_android_sdk_packages",
    "uninstall_android_sdk_package",
]
