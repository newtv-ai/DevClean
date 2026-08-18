"""Project-aware Bazel output inventory and vendor-owned cleanup."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_GIB = 1024**3
_RECOMMEND_BYTES = 2 * _GIB
_REPOSITORY_MARKERS = (
    "MODULE.bazel",
    "REPO.bazel",
    "WORKSPACE.bazel",
    "WORKSPACE",
)


class BazelCleanMode(StrEnum):
    CLEAN = "clean"
    EXPUNGE = "expunge"


@dataclass(frozen=True, slots=True)
class BazelWorkspaceInventory:
    workspace: Path
    output_base: Path
    logical_bytes: int
    executable: str
    release: str
    recommended_clean: bool
    expunge_user_review: bool = True


@dataclass(frozen=True, slots=True)
class BazelCleanResult:
    workspace: Path
    output_base: Path
    mode: BazelCleanMode
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inspect_bazel_workspace(
    workspace: Path,
    environment: Mapping[str, str] | None = None,
) -> BazelWorkspaceInventory:
    """Ask Bazel to identify a workspace and its exact output base."""

    root = _validated_workspace_path(workspace)
    executable = bazel_executable(environment)
    reported_workspace = _bazel_info(executable, root, "workspace", environment)
    if _normalized(Path(reported_workspace)) != _normalized(root):
        raise ValueError(
            "Bazel reported a different workspace root: "
            f"selected={root}, reported={reported_workspace}"
        )
    output_base_text = _bazel_info(executable, root, "output_base", environment)
    output_base = Path(output_base_text).expanduser()
    if not output_base.is_absolute():
        raise RuntimeError(f"Bazel output_base is not absolute: {output_base}")
    release = _bazel_info(executable, root, "release", environment)
    try:
        exists = output_base.is_dir()
    except OSError:
        exists = False
    size = _directory_bytes(output_base) if exists else 0
    return BazelWorkspaceInventory(
        workspace=root,
        output_base=output_base,
        logical_bytes=size,
        executable=executable,
        release=release,
        recommended_clean=exists and size >= _RECOMMEND_BYTES,
    )


def clean_bazel_workspace(
    workspace: Path,
    mode: BazelCleanMode,
    environment: Mapping[str, str] | None = None,
) -> BazelCleanResult:
    """Delegate one workspace cleanup to Bazel after exact revalidation."""

    inventory = inspect_bazel_workspace(workspace, environment)
    if bazel_client_process_running():
        raise RuntimeError("Bazel/Bazelisk client is already running; wait for it to finish")

    before = inventory.logical_bytes
    command = [inventory.executable, "clean"]
    if mode is BazelCleanMode.EXPUNGE:
        command.append("--expunge")
    result = _run_bazel(
        tuple(command),
        inventory.workspace,
        environment,
        timeout=1800,
    )

    try:
        after = (
            _directory_bytes(inventory.output_base)
            if inventory.output_base.is_dir()
            else 0
        )
    except OSError:
        after = 0
    return BazelCleanResult(
        workspace=inventory.workspace,
        output_base=inventory.output_base,
        mode=mode,
        before_bytes=before,
        after_bytes=after,
        command=tuple(command),
        output=(result.stdout or result.stderr).strip(),
    )


def bazel_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_bazel_exe")
    if configured:
        return configured
    if environment is None:
        for name in (
            "bazelisk.exe" if os.name == "nt" else "bazelisk",
            "bazel.exe" if os.name == "nt" else "bazel",
        ):
            located = shutil.which(name)
            if located:
                return located
    return "bazel.exe" if os.name == "nt" else "bazel"


def bazel_client_process_running() -> bool:
    """Block another Bazel client, but do not mistake the long-lived server for one."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -ieq 'bazel' -or $_.ProcessName -ieq 'bazelisk' }; "
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


def _validated_workspace_path(workspace: Path) -> Path:
    try:
        root = workspace.expanduser().resolve(strict=False)
    except OSError as error:
        raise ValueError(f"Cannot resolve Bazel workspace: {workspace}") from error
    if not root.is_absolute() or not root.is_dir():
        raise ValueError(f"Bazel workspace does not exist: {root}")
    if not any((root / marker).is_file() for marker in _REPOSITORY_MARKERS):
        raise ValueError(
            "Selected directory has no Bazel repository boundary file "
            f"({', '.join(_REPOSITORY_MARKERS)})"
        )
    return root


def _bazel_info(
    executable: str,
    workspace: Path,
    key: str,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_bazel(
        (executable, "info", key),
        workspace,
        environment,
        timeout=120,
    )
    lines = [line.strip().strip('"').strip("'") for line in result.stdout.splitlines()]
    values = [line for line in lines if line]
    if not values:
        raise RuntimeError(f"bazel info {key} returned no value")
    return values[-1]


def _run_bazel(
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
        raise RuntimeError(f"Cannot execute Bazel CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Bazel CLI failed (exit {result.returncode}): {detail}"
        )
    return result


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            base = Path(directory)
            for name in files:
                try:
                    total += (base / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "BazelCleanMode",
    "BazelCleanResult",
    "BazelWorkspaceInventory",
    "bazel_client_process_running",
    "bazel_executable",
    "clean_bazel_workspace",
    "inspect_bazel_workspace",
]
