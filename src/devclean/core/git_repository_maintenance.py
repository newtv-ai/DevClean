"""Project-aware Git repository and Git LFS vendor maintenance."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from devclean.platform.windows.volumes import is_local_fixed_path


@dataclass(frozen=True, slots=True)
class GitLfsInventory:
    available: bool
    version: str
    storage_dir: Path | None
    logical_bytes: int
    custom_storage: bool
    used: bool
    prune_supported: bool
    reason: str


@dataclass(frozen=True, slots=True)
class GitRepositoryInventory:
    workspace: Path
    executable: str
    version: str
    git_dir: Path
    common_dir: Path
    objects_dir: Path
    object_bytes: int
    alternates: tuple[Path, ...]
    maintenance_supported: bool
    maintenance_needed: bool | None
    maintenance_executable: bool
    maintenance_reason: str
    lfs: GitLfsInventory


@dataclass(frozen=True, slots=True)
class GitMaintenanceResult:
    workspace: Path
    objects_dir: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


@dataclass(frozen=True, slots=True)
class GitLfsPrunePreview:
    workspace: Path
    storage_dir: Path
    before_bytes: int
    command: tuple[str, ...]
    output: str


@dataclass(frozen=True, slots=True)
class GitLfsPruneResult:
    workspace: Path
    storage_dir: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_git_repository(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> GitRepositoryInventory:
    """Ask Git for the exact worktree and repository storage layout."""

    selected = _validated_directory(workspace)
    executable = git_executable(environment)
    reported = _absolute_git_path(
        _run_git(
            executable,
            selected,
            ("rev-parse", "--show-toplevel"),
            environment,
            timeout=30,
        ).stdout.strip(),
        selected,
        "worktree root",
    )
    if _normalized(reported) != _normalized(selected):
        raise ValueError(
            "所选目录不是 Git worktree 根目录: "
            f"selected={selected}, reported={reported}"
        )

    bare = _run_git(
        executable,
        selected,
        ("rev-parse", "--is-bare-repository"),
        environment,
        timeout=30,
    ).stdout.strip()
    if bare.casefold() == "true":
        raise ValueError("当前 Git 维护入口不处理 bare repository")

    git_dir = _absolute_git_path(
        _run_git(
            executable,
            selected,
            ("rev-parse", "--absolute-git-dir"),
            environment,
            timeout=30,
        ).stdout.strip(),
        selected,
        "git directory",
    )
    common_dir = _absolute_git_path(
        _run_git(
            executable,
            selected,
            ("rev-parse", "--path-format=absolute", "--git-common-dir"),
            environment,
            timeout=30,
        ).stdout.strip(),
        selected,
        "git common directory",
    )
    objects_dir = _absolute_git_path(
        _run_git(
            executable,
            selected,
            ("rev-parse", "--path-format=absolute", "--git-path", "objects"),
            environment,
            timeout=30,
        ).stdout.strip(),
        selected,
        "git objects directory",
    )
    count_output = _run_git(
        executable,
        selected,
        ("count-objects", "-v"),
        environment,
        timeout=60,
    ).stdout
    alternates = _parse_alternates(count_output, selected)
    object_bytes = _directory_bytes(objects_dir) if _is_directory(objects_dir) else 0
    version = _run_git(
        executable,
        selected,
        ("--version",),
        environment,
        timeout=30,
    ).stdout.strip() or "unknown"

    maintenance_supported, maintenance_needed = _maintenance_need(
        executable,
        selected,
        environment,
    )
    expected_objects = common_dir / "objects"
    local_boundary = all(
        is_local_fixed_path(path)
        for path in (selected, git_dir, common_dir, objects_dir)
    )
    standard_objects = _normalized(objects_dir) == _normalized(expected_objects)
    maintenance_executable = (
        maintenance_supported
        and local_boundary
        and standard_objects
        and not alternates
    )
    if not maintenance_supported:
        maintenance_reason = "当前 Git 无法确认 maintenance is-needed --auto; 仅报告"
    elif not local_boundary:
        maintenance_reason = "Git 工作区或元数据不在本机固定磁盘; 仅报告"
    elif not standard_objects:
        maintenance_reason = "Git object directory 已重定向; 不授予本地维护执行权限"
    elif alternates:
        maintenance_reason = "仓库使用 alternate object database; 不授予维护执行权限"
    elif maintenance_needed is True:
        maintenance_reason = "Git 自己判断已达到自动维护阈值"
    elif maintenance_needed is False:
        maintenance_reason = "Git 自己判断当前无需自动维护"
    else:
        maintenance_reason = "Git 自动维护状态未知"

    lfs = _inspect_lfs(executable, selected, common_dir, environment)
    return GitRepositoryInventory(
        workspace=selected,
        executable=executable,
        version=version,
        git_dir=git_dir,
        common_dir=common_dir,
        objects_dir=objects_dir,
        object_bytes=object_bytes,
        alternates=alternates,
        maintenance_supported=maintenance_supported,
        maintenance_needed=maintenance_needed,
        maintenance_executable=maintenance_executable,
        maintenance_reason=maintenance_reason,
        lfs=lfs,
    )


def run_git_automatic_maintenance(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> GitMaintenanceResult:
    """Run only Git's threshold-gated automatic maintenance for one exact worktree."""

    inventory = inspect_git_repository(workspace, environment)
    if not inventory.maintenance_executable:
        raise RuntimeError(inventory.maintenance_reason)
    if git_activity_running():
        raise RuntimeError("检测到 Git/Git LFS 活动; 请结束相关操作后再执行仓库维护")

    fresh = inspect_git_repository(workspace, environment)
    if not fresh.maintenance_executable:
        raise RuntimeError("Git 仓库布局或维护边界已变化; 请重新检查")

    command = (fresh.executable, "maintenance", "run", "--auto")
    before = fresh.object_bytes
    result = _run_git(
        fresh.executable,
        fresh.workspace,
        ("maintenance", "run", "--auto"),
        environment,
        timeout=1800,
    )
    after = _directory_bytes(fresh.objects_dir) if _is_directory(fresh.objects_dir) else 0
    return GitMaintenanceResult(
        workspace=fresh.workspace,
        objects_dir=fresh.objects_dir,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=(result.stdout or result.stderr).strip(),
    )


def preview_git_lfs_prune(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> GitLfsPrunePreview:
    """Ask Git LFS what its conservative verified prune would remove."""

    inventory = inspect_git_repository(workspace, environment)
    lfs = inventory.lfs
    if not lfs.prune_supported or lfs.storage_dir is None:
        raise RuntimeError(lfs.reason)
    command = (
        inventory.executable,
        "lfs",
        "prune",
        "--dry-run",
        "--verbose",
        "--verify-remote",
        "--verify-unreachable",
        "--when-unverified=halt",
    )
    result = _run_git(
        inventory.executable,
        inventory.workspace,
        command[1:],
        environment,
        timeout=1800,
    )
    return GitLfsPrunePreview(
        workspace=inventory.workspace,
        storage_dir=lfs.storage_dir,
        before_bytes=lfs.logical_bytes,
        command=command,
        output=(result.stdout or result.stderr).strip(),
    )


def run_git_lfs_prune(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> GitLfsPruneResult:
    """Run Git LFS's normal prune with remote verification and no force mode."""

    inventory = inspect_git_repository(workspace, environment)
    lfs = inventory.lfs
    if not lfs.prune_supported or lfs.storage_dir is None:
        raise RuntimeError(lfs.reason)
    if git_activity_running():
        raise RuntimeError("检测到 Git/Git LFS 活动; 请结束相关操作后再执行 LFS prune")

    fresh = inspect_git_repository(workspace, environment)
    lfs = fresh.lfs
    if not lfs.prune_supported or lfs.storage_dir is None:
        raise RuntimeError("Git LFS 存储边界已变化; 请重新检查")

    command = (
        fresh.executable,
        "lfs",
        "prune",
        "--verbose",
        "--verify-remote",
        "--verify-unreachable",
        "--when-unverified=halt",
    )
    before = lfs.logical_bytes
    result = _run_git(
        fresh.executable,
        fresh.workspace,
        command[1:],
        environment,
        timeout=3600,
    )
    after = _directory_bytes(lfs.storage_dir) if _is_directory(lfs.storage_dir) else 0
    return GitLfsPruneResult(
        workspace=fresh.workspace,
        storage_dir=lfs.storage_dir,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=(result.stdout or result.stderr).strip(),
    )


def git_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_git_exe")
    if configured:
        return configured
    if environment is None:
        name = "git.exe" if os.name == "nt" else "git"
        located = shutil.which(name)
        if located:
            return located
    return "git.exe" if os.name == "nt" else "git"


def git_activity_running() -> bool:
    """Fail closed on Windows while a Git-family process is active."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -match '^(git|git-lfs|git-remote-.*|git-upload-pack|"
        "git-receive-pack|scalar)$' }; if ($p) { 'RUNNING' }"
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


def _maintenance_need(
    executable: str,
    workspace: Path,
    environment: Mapping[str, str] | None,
) -> tuple[bool, bool | None]:
    result = _run_git_allow_status(
        executable,
        workspace,
        ("maintenance", "is-needed", "--auto"),
        environment,
        timeout=60,
    )
    if result.returncode == 0:
        return True, True
    if result.returncode == 1:
        return True, False
    return False, None


def _inspect_lfs(
    executable: str,
    workspace: Path,
    common_dir: Path,
    environment: Mapping[str, str] | None,
) -> GitLfsInventory:
    version_result = _run_git_allow_status(
        executable,
        workspace,
        ("lfs", "version"),
        environment,
        timeout=30,
    )
    if version_result.returncode != 0:
        return GitLfsInventory(
            available=False,
            version="not installed",
            storage_dir=None,
            logical_bytes=0,
            custom_storage=False,
            used=False,
            prune_supported=False,
            reason="未检测到可用的 Git LFS 客户端",
        )

    configured = _run_git_allow_status(
        executable,
        workspace,
        ("config", "--get", "lfs.storage"),
        environment,
        timeout=30,
    )
    custom_storage = configured.returncode == 0 and bool(configured.stdout.strip())
    env_result = _run_git_allow_status(
        executable,
        workspace,
        ("lfs", "env"),
        environment,
        timeout=60,
    )
    storage_dir = _lfs_media_dir(env_result.stdout, workspace) if env_result.returncode == 0 else None
    logical_bytes = (
        _directory_bytes(storage_dir)
        if storage_dir is not None and _is_directory(storage_dir)
        else 0
    )
    ls_files = _run_git_allow_status(
        executable,
        workspace,
        ("lfs", "ls-files", "--name-only"),
        environment,
        timeout=120,
    )
    used = logical_bytes > 0 or (ls_files.returncode == 0 and bool(ls_files.stdout.strip()))

    if custom_storage:
        supported = False
        reason = "检测到显式 lfs.storage; 该目录可能被多个仓库共享，当前仅报告"
    elif storage_dir is None:
        supported = False
        reason = "Git LFS 未返回可验证的 LocalMediaDir; 当前仅报告"
    elif _normalized(storage_dir) != _normalized(common_dir / "lfs"):
        supported = False
        reason = "Git LFS LocalMediaDir 不是仓库默认本地存储; 当前仅报告"
    elif not is_local_fixed_path(storage_dir):
        supported = False
        reason = "Git LFS 本地存储不在本机固定磁盘; 当前仅报告"
    elif not used:
        supported = False
        reason = "当前仓库未发现本地 Git LFS 对象，无需 prune"
    else:
        supported = True
        reason = (
            "Git LFS 可按自身 recent/unpushed/worktree 规则评估旧对象; "
            "删除仍由用户决定，并要求远端验证"
        )

    return GitLfsInventory(
        available=True,
        version=version_result.stdout.strip() or "unknown",
        storage_dir=storage_dir,
        logical_bytes=logical_bytes,
        custom_storage=custom_storage,
        used=used,
        prune_supported=supported,
        reason=reason,
    )


def _run_git(
    executable: str,
    workspace: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    result = _run_git_allow_status(executable, workspace, arguments, environment, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Git CLI 失败 (exit {result.returncode}): {detail}")
    return result


def _run_git_allow_status(
    executable: str,
    workspace: Path,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    command = [executable, "-C", str(workspace), *arguments]
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Git CLI: {error}") from error


def _validated_directory(path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 Git worktree: {path}") from error
    if not resolved.is_dir():
        raise ValueError(f"Git worktree 不存在: {resolved}")
    return resolved


def _absolute_git_path(value: str, workspace: Path, label: str) -> Path:
    if not value:
        raise RuntimeError(f"Git 未返回 {label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return Path(os.path.abspath(path))


def _parse_alternates(output: str, workspace: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "alternate" and value.strip():
            paths.append(_absolute_git_path(value.strip(), workspace, "alternate object database"))
    return tuple(paths)


def _lfs_media_dir(output: str, workspace: Path) -> Path | None:
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "LocalMediaDir" and value.strip():
            return _absolute_git_path(value.strip(), workspace, "Git LFS LocalMediaDir")
    return None


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


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "GitLfsInventory",
    "GitLfsPrunePreview",
    "GitLfsPruneResult",
    "GitMaintenanceResult",
    "GitRepositoryInventory",
    "git_activity_running",
    "git_executable",
    "inspect_git_repository",
    "preview_git_lfs_prune",
    "run_git_automatic_maintenance",
    "run_git_lfs_prune",
]
