"""Project-aware Cargo target-directory inventory and vendor cleanup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devclean.core.cargo_cleanup import cargo_process_running, clear_cargo_process_cache
from devclean.platform.windows.volumes import is_local_fixed_path

_GIB = 1024**3
_REVIEW_BYTES = 2 * _GIB


@dataclass(frozen=True, slots=True)
class CargoWorkspaceInventory:
    workspace: Path
    manifest: Path
    target_directory: Path
    logical_bytes: int
    executable: str
    version: str
    deletion_supported: bool
    worth_reviewing: bool
    user_review_required: bool = True


@dataclass(frozen=True, slots=True)
class CargoCleanResult:
    workspace: Path
    target_directory: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_cargo_workspace(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> CargoWorkspaceInventory:
    """Ask Cargo for the exact workspace root and effective target directory."""

    root = _validated_workspace(workspace)
    manifest = root / "Cargo.toml"
    executable = cargo_executable(environment)
    metadata = _cargo_metadata(executable, root, manifest, environment)

    reported_root = _absolute_metadata_path(metadata, "workspace_root")
    if _normalized(reported_root) != _normalized(root):
        raise ValueError(
            "所选目录不是 Cargo workspace 根目录: "
            f"selected={root}, reported={reported_root}"
        )
    target_directory = _absolute_metadata_path(metadata, "target_directory")
    exists = _is_directory(target_directory)
    logical_bytes = _directory_bytes(target_directory) if exists else 0
    deletion_supported = _safe_workspace_target(root, target_directory)
    version = _cargo_version(executable, root, environment)
    return CargoWorkspaceInventory(
        workspace=root,
        manifest=manifest,
        target_directory=target_directory,
        logical_bytes=logical_bytes,
        executable=executable,
        version=version,
        deletion_supported=deletion_supported,
        worth_reviewing=exists and logical_bytes >= _REVIEW_BYTES,
    )


def clean_cargo_workspace(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> CargoCleanResult:
    """Run Cargo's own clean command for one exact, locally bounded workspace target."""

    inventory = inspect_cargo_workspace(workspace, environment)
    if not inventory.deletion_supported:
        raise ValueError(
            "Cargo target_directory 不在所选 workspace 的本地固定磁盘边界内; "
            "可能是共享/外置构建目录，只允许检查"
        )
    clear_cargo_process_cache()
    if cargo_process_running():
        raise RuntimeError(
            "Cargo、rustc、rustup 或 rust-analyzer 正在运行; "
            "请结束相关构建/分析后再执行 cargo clean"
        )

    # Re-resolve once more immediately before the vendor mutation. Then pin the
    # target directory on the command line so a config edit cannot redirect the
    # clean to another path between inspection and execution.
    inventory = inspect_cargo_workspace(workspace, environment)
    if not inventory.deletion_supported:
        raise ValueError("Cargo target_directory 已变化; 请重新检查后再操作")

    command = (
        inventory.executable,
        "clean",
        "--manifest-path",
        str(inventory.manifest),
        "--target-dir",
        str(inventory.target_directory),
    )
    before = inventory.logical_bytes
    result = _run_cargo(command, inventory.workspace, environment, timeout=1800)
    after = (
        _directory_bytes(inventory.target_directory)
        if _is_directory(inventory.target_directory)
        else 0
    )
    return CargoCleanResult(
        workspace=inventory.workspace,
        target_directory=inventory.target_directory,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=(result.stdout or result.stderr).strip(),
    )


def cargo_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_cargo_exe")
    if configured:
        return configured
    if environment is None:
        name = "cargo.exe" if os.name == "nt" else "cargo"
        located = shutil.which(name)
        if located:
            return located
    return "cargo.exe" if os.name == "nt" else "cargo"


def _validated_workspace(workspace: Path) -> Path:
    try:
        root = workspace.expanduser().resolve(strict=True)
    except OSError as error:
        raise ValueError(f"无法解析 Cargo workspace: {workspace}") from error
    if not root.is_dir():
        raise ValueError(f"Cargo workspace 不存在: {root}")
    if not (root / "Cargo.toml").is_file():
        raise ValueError(f"所选目录没有 Cargo.toml: {root}")
    return root


def _cargo_metadata(
    executable: str,
    workspace: Path,
    manifest: Path,
    environment: Mapping[str, str] | None,
) -> dict[str, Any]:
    command = (
        executable,
        "metadata",
        "--format-version",
        "1",
        "--no-deps",
        "--manifest-path",
        str(manifest),
    )
    result = _run_cargo(command, workspace, environment, timeout=120)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("cargo metadata 没有返回有效 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError("cargo metadata 返回的顶层结构不是对象")
    return value


def _cargo_version(
    executable: str,
    workspace: Path,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_cargo((executable, "--version"), workspace, environment, timeout=30)
    return result.stdout.strip() or "unknown"


def _run_cargo(
    command: tuple[str, ...],
    workspace: Path,
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        result = subprocess.run(
            list(command),
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Cargo CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Cargo CLI 失败 (exit {result.returncode}): {detail}"
        )
    return result


def _absolute_metadata_path(metadata: Mapping[str, Any], key: str) -> Path:
    value = metadata.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"cargo metadata 缺少 {key}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeError(f"cargo metadata {key} 不是绝对路径: {value}")
    return Path(os.path.abspath(path))


def _safe_workspace_target(workspace: Path, target: Path) -> bool:
    if not is_local_fixed_path(workspace) or not is_local_fixed_path(target):
        return False
    try:
        common = Path(os.path.commonpath((str(workspace), str(target))))
    except ValueError:
        return False
    return (
        _normalized(common) == _normalized(workspace)
        and _normalized(target) != _normalized(workspace)
    )


def _is_directory(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


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


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items()}


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "CargoCleanResult",
    "CargoWorkspaceInventory",
    "cargo_executable",
    "clean_cargo_workspace",
    "inspect_cargo_workspace",
]
