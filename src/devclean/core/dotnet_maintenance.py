"""Vendor-backed .NET SDK workload maintenance.

DevClean does not infer workload pack directories and delete them directly.
Microsoft exposes ``dotnet workload clean`` specifically to garbage-collect
orphaned workload components left behind by SDK updates and uninstallations, so
this module delegates that decision to the installed .NET SDK.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from devclean.core.nuget_cleanup import dotnet_executable


@dataclass(frozen=True, slots=True)
class DotnetWorkloadCleanResult:
    command: tuple[str, ...]
    returncode: int
    output: str


RunFactory = Callable[..., subprocess.CompletedProcess[str]]


def run_dotnet_workload_clean(
    environment: Mapping[str, str] | None = None,
    *,
    runner: RunFactory = subprocess.run,
) -> DotnetWorkloadCleanResult:
    """Run the conservative vendor workload garbage collector.

    The public DevClean action deliberately does not expose ``--all``. Microsoft's
    documentation describes plain ``dotnet workload clean`` as garbage collection
    for orphaned workload packs, while ``--all`` is the more aggressive mode that
    removes every pack of the current SDK workload installation type and associated
    installation records.
    """

    clear_dotnet_process_cache()
    if dotnet_sdk_process_running():
        raise RuntimeError(
            ".NET SDK、MSBuild 或 Visual Studio 正在运行；关闭相关进程后再清理 workload"
        )

    command = (dotnet_executable(environment), "workload", "clean")
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        completed = runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1_200,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 dotnet workload clean：{error}") from error

    output = _combined_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError(
            "dotnet workload clean 失败 "
            f"(退出码 {completed.returncode})：{output or '没有输出'}"
        )
    return DotnetWorkloadCleanResult(
        command=command,
        returncode=completed.returncode,
        output=output,
    )


def dotnet_sdk_process_running() -> bool:
    """Fail closed while common .NET/Visual Studio build owners are active."""

    return _dotnet_sdk_process_running_cached()


@lru_cache(maxsize=1)
def _dotnet_sdk_process_running_cached() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -match '(?i)^(?:dotnet|msbuild|devenv)$' "
        "}; if ($p) { 'RUNNING' }"
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


def clear_dotnet_process_cache() -> None:
    _dotnet_sdk_process_running_cached.cache_clear()


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()
    )


__all__ = [
    "DotnetWorkloadCleanResult",
    "clear_dotnet_process_cache",
    "dotnet_sdk_process_running",
    "run_dotnet_workload_clean",
]
