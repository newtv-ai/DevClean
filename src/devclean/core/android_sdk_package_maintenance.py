"""Exact Android SDK package inventory, AVD correlation, and vendor uninstall."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.core.android_avd_cleanup import android_avd_roots
from devclean.core.android_sdk_cleanup import (
    android_sdk_process_running,
    android_sdk_roots,
    clear_android_sdk_process_cache,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_PACKAGE_ID = re.compile(r"^[A-Za-z0-9._+\-]+(?:;[A-Za-z0-9._+\-]+)*$")
_SYSTEM_IMAGE_PREFIX = "system-images;"


@dataclass(frozen=True, slots=True)
class AndroidPathIdentity:
    volume_serial: int
    file_id: str
    file_id_kind: str
    creation_time_ns: int | None
    last_write_time_ns: int | None


@dataclass(frozen=True, slots=True)
class AndroidAvdSystemReference:
    avd_name: str
    content_root: Path
    config_path: Path
    raw_system_dirs: tuple[str, ...]
    resolved_system_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class AndroidSdkPackageEntry:
    sdk_root: Path
    package_id: str
    version: str
    description: str
    location: str
    installed_path: Path | None
    installed_identity: AndroidPathIdentity | None
    logical_bytes: int
    avd_names: tuple[str, ...]
    deletion_supported: bool
    protected_reason: str = ""


@dataclass(frozen=True, slots=True)
class AndroidSdkRootInventory:
    sdk_root: Path
    sdk_root_identity: AndroidPathIdentity | None
    sdkmanager: Path | None
    sdkmanager_identity: AndroidPathIdentity | None
    local_fixed: bool
    packages: tuple[AndroidSdkPackageEntry, ...]
    avd_reference_proof_complete: bool
    avd_reference_proof_reason: str
    error: str = ""

    @property
    def package_bytes(self) -> int:
        return sum(package.logical_bytes for package in self.packages)


@dataclass(frozen=True, slots=True)
class AndroidSdkInventory:
    roots: tuple[AndroidSdkRootInventory, ...]
    avd_references: tuple[AndroidAvdSystemReference, ...]
    avd_reference_proof_complete: bool
    avd_reference_proof_reason: str

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
    """Inventory installed SDK packages and correlate static AVD system images."""

    source_roots = _source_sdk_roots(environment)
    avds, avd_complete, avd_reason = _inventory_avd_references(source_roots, environment)
    roots: list[AndroidSdkRootInventory] = []
    for root in source_roots:
        try:
            roots.append(
                _inventory_root(
                    root,
                    avds,
                    avd_complete,
                    avd_reason,
                    environment,
                )
            )
        except Exception as error:
            roots.append(
                AndroidSdkRootInventory(
                    sdk_root=root,
                    sdk_root_identity=None,
                    sdkmanager=None,
                    sdkmanager_identity=None,
                    local_fixed=_safe_local_fixed(root),
                    packages=(),
                    avd_reference_proof_complete=avd_complete,
                    avd_reference_proof_reason=avd_reason,
                    error=str(error),
                )
            )
    return AndroidSdkInventory(
        roots=tuple(roots),
        avd_references=avds,
        avd_reference_proof_complete=avd_complete,
        avd_reference_proof_reason=avd_reason,
    )


def uninstall_android_sdk_package(
    expected: AndroidSdkPackageEntry,
    expected_root: AndroidSdkRootInventory,
    environment: Mapping[str, str] | None = None,
) -> AndroidSdkUninstallResult:
    """Uninstall one exact reviewed package through its exact SDK-owned sdkmanager."""

    if not _PACKAGE_ID.fullmatch(expected.package_id):
        raise ValueError(f"Android SDK package id 无效: {expected.package_id}")
    if expected_root.sdkmanager is None or expected_root.sdkmanager_identity is None:
        raise RuntimeError("用户查看的 Android SDK 没有可验证的 sdkmanager")
    if expected_root.sdk_root_identity is None:
        raise RuntimeError("用户查看的 Android SDK 根没有稳定身份")
    if not expected.deletion_supported:
        raise ValueError(expected.protected_reason or "该 Android SDK package 当前不可卸载")

    clear_android_sdk_process_cache()
    if android_sdk_process_running() or _android_runtime_process_running():
        raise RuntimeError(
            "Android Studio、Gradle/sdkmanager、ADB 或 Emulator 正在使用 SDK；"
            "请关闭相关工具后再卸载 package"
        )

    current = _inventory_exact_root(expected_root.sdk_root, environment)
    _require_same_root(expected_root, current)
    selected = _exact_package(current.packages, expected.package_id)
    _require_same_package(expected, selected)
    if not selected.deletion_supported:
        raise RuntimeError(selected.protected_reason or "package 已失去安全卸载权限")

    # Recheck process state after the vendor listing and AVD correlation work.
    clear_android_sdk_process_cache()
    if android_sdk_process_running() or _android_runtime_process_running():
        raise RuntimeError("Android SDK 在执行前变为使用中；拒绝卸载")

    fresh = _inventory_exact_root(expected_root.sdk_root, environment)
    _require_same_root(current, fresh)
    selected = _exact_package(fresh.packages, expected.package_id)
    _require_same_package(expected, selected)
    if not selected.deletion_supported or fresh.sdkmanager is None:
        raise RuntimeError(selected.protected_reason or "package 已失去安全卸载权限")

    before = selected.logical_bytes
    command = (
        str(fresh.sdkmanager),
        "--uninstall",
        selected.package_id,
        f"--sdk_root={fresh.sdk_root}",
    )
    result = _run_sdkmanager(command, environment, timeout=1800)

    after_inventory = _inventory_exact_root(fresh.sdk_root, environment)
    if after_inventory.sdk_root_identity != fresh.sdk_root_identity:
        raise RuntimeError("Android SDK 根身份在卸载后发生变化；无法确认后置状态")
    if any(package.package_id == selected.package_id for package in after_inventory.packages):
        raise RuntimeError(
            f"sdkmanager 返回成功，但 package 仍被列为已安装: {selected.package_id}"
        )
    after = _path_bytes(selected.installed_path)
    return AndroidSdkUninstallResult(
        sdk_root=fresh.sdk_root,
        package_id=selected.package_id,
        version=selected.version,
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
    sources = _source_sdk_roots(environment)
    if not any(_normalized(root) == _normalized(candidate) for candidate in sources):
        raise ValueError(f"不是当前来源可确认的 Android SDK 根: {root}")
    avds, complete, reason = _inventory_avd_references(sources, environment)
    return _inventory_root(root, avds, complete, reason, environment)


def _inventory_root(
    root: Path,
    avds: tuple[AndroidAvdSystemReference, ...],
    avd_complete: bool,
    avd_reason: str,
    environment: Mapping[str, str] | None,
) -> AndroidSdkRootInventory:
    root = _ordinary_directory(root, "Android SDK 根")
    root_before = _identity(root, require_directory=True)
    local_fixed = is_local_fixed_path(root)
    sdkmanager = _find_sdkmanager(root)
    if sdkmanager is None:
        return AndroidSdkRootInventory(
            sdk_root=root,
            sdk_root_identity=root_before,
            sdkmanager=None,
            sdkmanager_identity=None,
            local_fixed=local_fixed,
            packages=(),
            avd_reference_proof_complete=avd_complete,
            avd_reference_proof_reason=avd_reason,
            error="没有找到这个 SDK 自己的普通本地 sdkmanager；只报告根目录，不提供卸载",
        )
    sdkmanager_before = _identity(sdkmanager, require_directory=False)
    result = _run_sdkmanager(
        (str(sdkmanager), "--list", f"--sdk_root={root}"),
        environment,
        timeout=240,
    )
    packages = _parse_installed_packages(
        result.stdout,
        root,
        local_fixed,
        avds,
        avd_complete,
        avd_reason,
    )
    root_after = _identity(root, require_directory=True)
    sdkmanager_after = _identity(sdkmanager, require_directory=False)
    if root_before != root_after or sdkmanager_before != sdkmanager_after:
        raise RuntimeError("Android SDK 根或 sdkmanager 身份在检查期间发生变化")
    return AndroidSdkRootInventory(
        sdk_root=root,
        sdk_root_identity=root_after,
        sdkmanager=sdkmanager,
        sdkmanager_identity=sdkmanager_after,
        local_fixed=local_fixed,
        packages=packages,
        avd_reference_proof_complete=avd_complete,
        avd_reference_proof_reason=avd_reason,
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
            ordinary = _ordinary_file(candidate, "sdkmanager")
            if not _strict_descendant(ordinary, sdk_root):
                continue
            if not is_local_fixed_path(ordinary):
                continue
            return ordinary
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _parse_installed_packages(
    output: str,
    sdk_root: Path,
    root_local_fixed: bool,
    avds: tuple[AndroidAvdSystemReference, ...],
    avd_complete: bool,
    avd_reason: str,
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
        installed_identity: AndroidPathIdentity | None = None
        path_safe = False
        if installed_path is not None:
            try:
                installed_path = _ordinary_existing_path(installed_path, "Android SDK package path")
                installed_identity = _identity_any(installed_path)
                path_safe = is_local_fixed_path(installed_path)
            except (OSError, RuntimeError, ValueError):
                path_safe = False

        avd_names = (
            _matching_avd_names(installed_path, avds)
            if package_id.casefold().startswith(_SYSTEM_IMAGE_PREFIX)
            and installed_path is not None
            else ()
        )
        protected_reason = _protected_package_reason(
            package_id,
            path_safe=path_safe,
            root_local_fixed=root_local_fixed,
            avd_names=avd_names,
            avd_complete=avd_complete,
            avd_reason=avd_reason,
        )
        entries.append(
            AndroidSdkPackageEntry(
                sdk_root=sdk_root,
                package_id=package_id,
                version=version,
                description=description,
                location=location,
                installed_path=installed_path,
                installed_identity=installed_identity,
                logical_bytes=_path_bytes(installed_path),
                avd_names=avd_names,
                deletion_supported=not protected_reason,
                protected_reason=protected_reason,
            )
        )
    if not in_table:
        raise RuntimeError("sdkmanager Installed packages 表缺少列标题")
    entries.sort(key=lambda package: package.logical_bytes, reverse=True)
    return tuple(entries)


def _protected_package_reason(
    package_id: str,
    *,
    path_safe: bool,
    root_local_fixed: bool,
    avd_names: tuple[str, ...],
    avd_complete: bool,
    avd_reason: str,
) -> str:
    folded = package_id.casefold()
    if folded == "tools" or folded.startswith("cmdline-tools;"):
        return "这是 sdkmanager 自身所属的命令行工具 package；不能让执行器卸载自己"
    if not root_local_fixed:
        return "Android SDK 根不在可批准的本地固定磁盘边界内"
    if not path_safe:
        return "package Location 不能证明为 SDK 根内普通本地对象"
    if folded.startswith(_SYSTEM_IMAGE_PREFIX):
        if not avd_complete:
            return "AVD system-image 引用证明不完整；" + (avd_reason or "拒绝卸载 system image")
        if avd_names:
            names = ", ".join(avd_names[:4])
            suffix = "…" if len(avd_names) > 4 else ""
            return f"system image 正被 {len(avd_names)} 个 AVD 配置引用：{names}{suffix}"
    return ""


def _inventory_avd_references(
    sdk_roots: tuple[Path, ...],
    environment: Mapping[str, str] | None,
) -> tuple[tuple[AndroidAvdSystemReference, ...], bool, str]:
    references: list[AndroidAvdSystemReference] = []
    errors: list[str] = []
    for raw_content in _source_avd_content_roots(environment):
        content = Path(os.path.abspath(os.fspath(raw_content)))
        try:
            content = _ordinary_directory(content, "AVD content root")
            config = _ordinary_file(content / "config.ini", "AVD config.ini")
            before = _identity(config, require_directory=False)
            values = _read_ini(config)
            after = _identity(config, require_directory=False)
            if before != after:
                raise RuntimeError("config.ini 在读取期间发生变化")
            raw_dirs = tuple(
                value
                for key in ("image.sysdir.1", "image.sysdir.2")
                if (value := values.get(key))
            )
            if not raw_dirs:
                raise RuntimeError("config.ini 缺少 image.sysdir.1/2")
            resolved: list[Path] = []
            for value in raw_dirs:
                resolved.extend(_resolve_system_dir_candidates(value, sdk_roots))
            if not resolved:
                raise RuntimeError("AVD system-image 路径无法解析")
            avd_name = (
                values.get("avdid")
                or values.get("avd.ini.displayname")
                or content.stem
            )
            references.append(
                AndroidAvdSystemReference(
                    avd_name=avd_name,
                    content_root=content,
                    config_path=config,
                    raw_system_dirs=raw_dirs,
                    resolved_system_dirs=_unique_paths(resolved),
                )
            )
        except Exception as error:
            errors.append(f"{content}: {error}")
    references.sort(key=lambda item: item.avd_name.casefold())
    if errors:
        preview = "; ".join(errors[:3])
        if len(errors) > 3:
            preview += f"; 另有 {len(errors) - 3} 个错误"
        return tuple(references), False, preview
    return tuple(references), True, ""


def _resolve_system_dir_candidates(value: str, sdk_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    raw = value.strip().strip('"').strip("'")
    if not raw:
        return ()
    windows_path = PureWindowsPath(raw)
    native = Path(raw)
    if windows_path.is_absolute() or native.is_absolute():
        candidate = Path(raw.replace("\\", os.sep).replace("/", os.sep))
        return (Path(os.path.abspath(candidate)),)
    parts = tuple(part for part in re.split(r"[\\/]+", raw) if part not in {"", "."})
    if not parts:
        return ()
    return tuple(Path(os.path.abspath(root.joinpath(*parts))) for root in sdk_roots)


def _matching_avd_names(
    installed_path: Path,
    references: tuple[AndroidAvdSystemReference, ...],
) -> tuple[str, ...]:
    target = _normalized(installed_path)
    names = {
        reference.avd_name
        for reference in references
        if any(_normalized(candidate) == target for candidate in reference.resolved_system_dirs)
    }
    return tuple(sorted(names, key=str.casefold))


def _read_ini(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"无法读取 AVD config.ini: {error}") from error
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip().casefold()
        if normalized_key in values:
            raise RuntimeError(f"AVD config.ini 包含重复 key: {normalized_key}")
        values[normalized_key] = value.strip().strip('"').strip("'")
    return values


def _resolve_package_location(sdk_root: Path, location: str) -> Path | None:
    if not location:
        return None
    raw = location.strip().strip('"').strip("'")
    native = Path(raw.replace("\\", os.sep).replace("/", os.sep))
    path = native if native.is_absolute() else sdk_root / native
    path = Path(os.path.abspath(path))
    if not _strict_descendant(path, sdk_root):
        return None
    return path


def _require_same_root(expected: AndroidSdkRootInventory, current: AndroidSdkRootInventory) -> None:
    if (
        _normalized(expected.sdk_root) != _normalized(current.sdk_root)
        or expected.sdk_root_identity != current.sdk_root_identity
        or expected.sdkmanager != current.sdkmanager
        or expected.sdkmanager_identity != current.sdkmanager_identity
    ):
        raise RuntimeError("Android SDK 根或 sdkmanager 身份在用户确认后发生变化")


def _require_same_package(expected: AndroidSdkPackageEntry, current: AndroidSdkPackageEntry) -> None:
    if (
        current.package_id != expected.package_id
        or current.version != expected.version
        or current.location != expected.location
        or current.installed_path != expected.installed_path
        or current.installed_identity != expected.installed_identity
        or current.avd_names != expected.avd_names
    ):
        raise RuntimeError("Android SDK package identity/version/Location/AVD 引用已变化；请重新检查")


def _exact_package(
    packages: Sequence[AndroidSdkPackageEntry],
    package_id: str,
) -> AndroidSdkPackageEntry:
    matches = [package for package in packages if package.package_id == package_id]
    if len(matches) != 1:
        raise RuntimeError(f"无法唯一确认 Android SDK package {package_id!r}: found={len(matches)}")
    return matches[0]


def _run_sdkmanager(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update({str(key): str(value) for key, value in environment.items()})
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    existing_java_options = env.get("JAVA_TOOL_OPTIONS", "").strip()
    locale_options = "-Duser.language=en -Duser.country=US"
    env["JAVA_TOOL_OPTIONS"] = (
        f"{existing_java_options} {locale_options}".strip()
        if existing_java_options
        else locale_options
    )
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
        raise RuntimeError(f"sdkmanager 失败 (exit {result.returncode}): {detail}")
    return result


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


def _ordinary_directory(path: Path, label: str) -> Path:
    return _ordinary_path(path, label, require_directory=True)


def _ordinary_file(path: Path, label: str) -> Path:
    return _ordinary_path(path, label, require_directory=False)


def _ordinary_existing_path(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path.expanduser()))
    try:
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError(f"{label} 不能是 symlink/junction/reparse")
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 {label}: {candidate}") from error
    if _normalized(candidate) != _normalized(resolved):
        raise ValueError(f"{label} 存在路径重定向/reparse")
    metadata = read_file_metadata(candidate)
    if metadata.is_reparse_point:
        raise ValueError(f"{label} 是 reparse point")
    return resolved


def _ordinary_path(path: Path, label: str, *, require_directory: bool) -> Path:
    resolved = _ordinary_existing_path(path, label)
    metadata = read_file_metadata(resolved)
    if metadata.is_directory != require_directory:
        expected = "目录" if require_directory else "文件"
        raise ValueError(f"{label} 不是普通{expected}: {resolved}")
    return resolved


def _identity(path: Path, *, require_directory: bool) -> AndroidPathIdentity:
    metadata = read_file_metadata(path)
    if metadata.is_directory != require_directory or metadata.is_reparse_point:
        raise RuntimeError("对象类型/reparse 状态不符合稳定身份要求")
    return _identity_from_metadata(metadata)


def _identity_any(path: Path) -> AndroidPathIdentity:
    metadata = read_file_metadata(path)
    if metadata.is_reparse_point:
        raise RuntimeError("对象是 reparse point")
    return _identity_from_metadata(metadata)


def _identity_from_metadata(metadata: object) -> AndroidPathIdentity:
    volume_serial = getattr(metadata, "volume_serial", None)
    file_id = getattr(metadata, "file_id", None)
    file_id_kind = getattr(metadata, "file_id_kind", None)
    if volume_serial is None or file_id is None or file_id_kind is None:
        raise RuntimeError("对象没有可验证的稳定文件身份")
    return AndroidPathIdentity(
        volume_serial=int(volume_serial),
        file_id=str(file_id),
        file_id_kind=str(file_id_kind),
        creation_time_ns=getattr(metadata, "creation_time_ns", None),
        last_write_time_ns=getattr(metadata, "last_write_time_ns", None),
    )


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


def _source_sdk_roots(environment: Mapping[str, str] | None) -> tuple[Path, ...]:
    return _unique_paths(Path(str(path)) for path in android_sdk_roots(environment))


def _source_avd_content_roots(environment: Mapping[str, str] | None) -> tuple[Path, ...]:
    return _unique_paths(Path(str(path)) for path in android_avd_roots(environment).content_roots)


def _strict_descendant(path: Path, root: Path) -> bool:
    try:
        common = Path(os.path.commonpath((str(root), str(path))))
    except ValueError:
        return False
    return _normalized(common) == _normalized(root) and _normalized(path) != _normalized(root)


def _unique_paths(paths: Sequence[Path] | object) -> tuple[Path, ...]:
    # Accept generators without exposing a broad Iterable type to callers.
    values = tuple(paths)  # type: ignore[arg-type]
    found: list[Path] = []
    seen: set[str] = set()
    for path in values:
        candidate = Path(path)
        key = _normalized(candidate)
        if key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return tuple(found)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path))).casefold()


def _safe_local_fixed(path: Path) -> bool:
    try:
        return is_local_fixed_path(path)
    except OSError:
        return False


__all__ = [
    "AndroidAvdSystemReference",
    "AndroidPathIdentity",
    "AndroidSdkInventory",
    "AndroidSdkPackageEntry",
    "AndroidSdkRootInventory",
    "AndroidSdkUninstallResult",
    "inventory_android_sdk_packages",
    "uninstall_android_sdk_package",
]
