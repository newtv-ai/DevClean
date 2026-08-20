r"""Source-bound Cypress binary-cache inventory and vendor prune maintenance.

Cypress's cache is shared across projects. DevClean therefore never infers that an
older cached binary is unused merely from its age. The only destructive action in
this module is Cypress's own ``cache prune`` command after exact root/tool review.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_SEMVER_DIR_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_EXTERNAL_CACHE_ENTRIES = frozenset({"bundles", "sessions"})


@dataclass(frozen=True, slots=True)
class CypressPathIdentity:
    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str
    is_directory: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None


@dataclass(frozen=True, slots=True)
class CypressBinaryCacheEntry:
    version: str
    path: Path
    identity: CypressPathIdentity
    logical_bytes: int
    file_count: int
    current_package_version: bool


@dataclass(frozen=True, slots=True)
class CypressStorageInventory:
    cli_tool: CypressPathIdentity
    cache_root: Path
    cache_root_identity: CypressPathIdentity | None
    package_version: str
    versions: tuple[CypressBinaryCacheEntry, ...]
    external_entries: tuple[str, ...]
    unknown_entries: tuple[str, ...]

    @property
    def binary_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.versions)

    @property
    def prune_candidates(self) -> tuple[CypressBinaryCacheEntry, ...]:
        return tuple(item for item in self.versions if not item.current_package_version)

    @property
    def prune_candidate_bytes(self) -> int:
        return sum(item.logical_bytes for item in self.prune_candidates)

    @property
    def prune_supported(self) -> bool:
        return self.cache_root_identity is not None and not self.unknown_entries


@dataclass(frozen=True, slots=True)
class CypressPruneResult:
    cache_root: Path
    package_version: str
    removed_versions: tuple[str, ...]
    before_binary_bytes: int
    after_binary_bytes: int
    stdout: str

    @property
    def logical_reclaimed_bytes(self) -> int:
        return max(0, self.before_binary_bytes - self.after_binary_bytes)


def inventory_cypress_storage(
    cli_path: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> CypressStorageInventory:
    """Inventory one exact Cypress CLI package and the cache root it reports."""

    tool = _resolve_cypress_tool(cli_path, environment)
    discovery_env = _base_environment(environment)
    package_version = _package_version(tool, discovery_env)
    cache_root = _cache_path(tool, discovery_env)

    pinned_env = _cypress_environment(cache_root, environment)
    confirmed_root = _cache_path(tool, pinned_env)
    if _normalize(confirmed_root) != _normalize(cache_root):
        raise RuntimeError("Cypress 未确认固定的 cache 根目录; 已安全停止")
    confirmed_version = _package_version(tool, pinned_env)
    if confirmed_version != package_version:
        raise RuntimeError("Cypress package version 在 inventory 期间发生变化")

    root_identity = _optional_root_identity(cache_root)
    versions: list[CypressBinaryCacheEntry] = []
    external: list[str] = []
    unknown: list[str] = []

    if root_identity is not None:
        try:
            children = sorted(cache_root.iterdir(), key=lambda path: path.name.casefold())
        except OSError as error:
            raise RuntimeError(f"无法读取 Cypress cache 根目录: {error}") from error

        for child in children:
            if child.name in _EXTERNAL_CACHE_ENTRIES:
                external.append(child.name)
                continue
            if not _SEMVER_DIR_RE.fullmatch(child.name):
                unknown.append(child.name)
                continue
            try:
                identity = _path_identity(
                    child,
                    expect_directory=True,
                    label=f"Cypress cache version {child.name}",
                )
            except (OSError, RuntimeError):
                unknown.append(child.name)
                continue
            logical_bytes, file_count = _directory_stats(child)
            versions.append(
                CypressBinaryCacheEntry(
                    version=child.name,
                    path=identity.path,
                    identity=identity,
                    logical_bytes=logical_bytes,
                    file_count=file_count,
                    current_package_version=child.name == package_version,
                )
            )

    fresh_tool = _path_identity(tool.path, expect_directory=False, label="Cypress CLI")
    if fresh_tool != tool:
        raise RuntimeError("Cypress CLI 身份在 inventory 期间发生变化")
    if root_identity is not None:
        fresh_root = _path_identity(cache_root, expect_directory=True, label="Cypress cache root")
        if fresh_root != root_identity:
            raise RuntimeError("Cypress cache root 身份在 inventory 期间发生变化")

    versions.sort(key=lambda item: item.version.casefold())
    return CypressStorageInventory(
        cli_tool=tool,
        cache_root=cache_root,
        cache_root_identity=root_identity,
        package_version=package_version,
        versions=tuple(versions),
        external_entries=tuple(sorted(external, key=str.casefold)),
        unknown_entries=tuple(sorted(set(unknown), key=str.casefold)),
    )


def prune_cypress_binary_cache(
    reviewed: CypressStorageInventory,
    cli_path: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> CypressPruneResult:
    """Run exact Cypress ``cache prune`` after revalidating the reviewed scope."""

    current = inventory_cypress_storage(cli_path, environment)
    _require_same_review(reviewed, current)
    if not current.prune_supported:
        raise RuntimeError("Cypress cache 含未知/不稳定顶层对象; 拒绝运行 vendor prune")
    candidates = current.prune_candidates
    if not candidates:
        raise RuntimeError("当前没有其他 Cypress binary cache 版本需要 prune")

    _require_process_idle()
    fresh = inventory_cypress_storage(cli_path, environment)
    _require_same_review(current, fresh)
    if not fresh.prune_supported:
        raise RuntimeError("Cypress cache 范围在执行前变得不安全; 请重新检查")
    _require_process_idle()

    removed_versions = tuple(item.version for item in fresh.prune_candidates)
    before_bytes = fresh.binary_bytes
    result = _run_cypress(
        fresh.cli_tool,
        ("cache", "prune"),
        _cypress_environment(fresh.cache_root, environment),
        timeout=600,
    )
    _require_success(result, "cypress cache prune")

    after = inventory_cypress_storage(cli_path, environment)
    _require_same_boundaries(fresh, after)
    if after.unknown_entries:
        raise RuntimeError("Cypress prune 后出现未知 cache 顶层对象; 不报告成功")
    remaining = {item.version for item in after.versions}
    still_present = sorted(set(removed_versions) & remaining, key=str.casefold)
    if still_present:
        joined = ", ".join(still_present)
        raise RuntimeError(f"Cypress 返回成功后旧 binary cache 仍存在: {joined}")
    unexpected = [
        item.version
        for item in after.versions
        if item.version != after.package_version
    ]
    if unexpected:
        joined = ", ".join(sorted(unexpected, key=str.casefold))
        raise RuntimeError(f"Cypress prune 后仍出现非当前版本 cache: {joined}")

    return CypressPruneResult(
        cache_root=fresh.cache_root,
        package_version=fresh.package_version,
        removed_versions=removed_versions,
        before_binary_bytes=before_bytes,
        after_binary_bytes=after.binary_bytes,
        stdout=_combined_output(result.stdout, result.stderr),
    )


def _require_same_review(
    reviewed: CypressStorageInventory,
    current: CypressStorageInventory,
) -> None:
    _require_same_boundaries(reviewed, current)
    if reviewed.versions != current.versions:
        raise RuntimeError("Cypress binary cache 自审核后发生变化; 请重新统计")
    if reviewed.external_entries != current.external_entries:
        raise RuntimeError("Cypress 外部 cache 状态自审核后发生变化; 请重新统计")
    if reviewed.unknown_entries != current.unknown_entries:
        raise RuntimeError("Cypress 未知 cache 对象自审核后发生变化; 请重新统计")


def _require_same_boundaries(
    reviewed: CypressStorageInventory,
    current: CypressStorageInventory,
) -> None:
    if reviewed.cli_tool != current.cli_tool:
        raise RuntimeError("Cypress CLI 身份自审核后发生变化")
    if reviewed.package_version != current.package_version:
        raise RuntimeError("Cypress package version 自审核后发生变化")
    if _normalize(reviewed.cache_root) != _normalize(current.cache_root):
        raise RuntimeError("Cypress cache 根目录自审核后发生变化")
    if reviewed.cache_root_identity != current.cache_root_identity:
        raise RuntimeError("Cypress cache 根目录身份自审核后发生变化")


def _resolve_cypress_tool(
    cli_path: str | os.PathLike[str] | None,
    environment: Mapping[str, str] | None,
) -> CypressPathIdentity:
    source = os.environ if environment is None else environment
    folded = {str(key).casefold(): str(value) for key, value in source.items() if value}
    raw = str(cli_path) if cli_path is not None else folded.get("devclean_cypress_cli")
    if not raw:
        search_path = folded.get("path")
        candidates = ("cypress.cmd", "cypress.exe", "cypress") if os.name == "nt" else ("cypress",)
        resolved = next(
            (candidate for name in candidates if (candidate := shutil.which(name, path=search_path))),
            None,
        )
        if resolved is None:
            raise FileNotFoundError(
                "未找到 Cypress CLI; 请选择已安装的 cypress/cypress.cmd，DevClean 不会调用 npx 下载"
            )
        raw = resolved
    return _path_identity(Path(raw), expect_directory=False, label="Cypress CLI")


def _package_version(
    tool: CypressPathIdentity,
    environment: Mapping[str, str],
) -> str:
    result = _run_cypress(tool, ("version", "--component", "package"), environment, timeout=45)
    _require_success(result, "cypress version --component package")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or not _SEMVER_DIR_RE.fullmatch(lines[0]):
        raise RuntimeError("无法从 Cypress CLI 获得唯一的 semver package version")
    return lines[0]


def _cache_path(
    tool: CypressPathIdentity,
    environment: Mapping[str, str],
) -> Path:
    result = _run_cypress(tool, ("cache", "path"), environment, timeout=45)
    _require_success(result, "cypress cache path")
    lines = [line.strip().strip('"').strip("'") for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("cypress cache path 没有返回唯一路径")
    raw = lines[0]
    windows = PureWindowsPath(raw)
    if windows.is_absolute():
        return Path(str(windows))
    native = Path(raw).expanduser()
    if native.is_absolute():
        return Path(os.path.abspath(native))
    raise RuntimeError(f"Cypress 返回的 cache 路径不是绝对路径: {raw}")


def _optional_root_identity(path: Path) -> CypressPathIdentity | None:
    try:
        exists = path.is_dir()
    except OSError:
        exists = False
    if not exists:
        return None
    return _path_identity(path, expect_directory=True, label="Cypress cache root")


def _path_identity(
    path: Path,
    *,
    expect_directory: bool,
    label: str,
) -> CypressPathIdentity:
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
    return CypressPathIdentity(
        path=resolved,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        is_directory=metadata.is_directory,
        creation_time_ns=None if expect_directory else metadata.creation_time_ns,
        last_write_time_ns=None if expect_directory else metadata.last_write_time_ns,
    )


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


def _run_cypress(
    tool: CypressPathIdentity,
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
        raise RuntimeError(f"无法执行 Cypress CLI: {error}") from error


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


def _cypress_environment(
    cache_root: Path,
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    env = _base_environment(environment)
    for key in tuple(env):
        if key.casefold() in {
            "cypress_cache_folder",
            "npm_config_cypress_cache_folder",
            "npm_package_config_cypress_cache_folder",
        }:
            del env[key]
    env["CYPRESS_CACHE_FOLDER"] = str(cache_root)
    return env


def _require_process_idle() -> None:
    if os.name != "nt":
        return
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'Cypress.exe' -or "
        "($_.Name -ieq 'node.exe' -and $_.CommandLine -match '(?i)cypress') }; "
        "if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("无法确认 Cypress 进程状态; 拒绝修改 cache") from error
    if result.returncode != 0 or "RUNNING" in result.stdout:
        raise RuntimeError("Cypress 正在运行或进程状态无法确认; 拒绝修改 cache")


def _normalize(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip())


__all__ = [
    "CypressBinaryCacheEntry",
    "CypressPathIdentity",
    "CypressPruneResult",
    "CypressStorageInventory",
    "inventory_cypress_storage",
    "prune_cypress_binary_cache",
]
