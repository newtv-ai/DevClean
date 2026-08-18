"""User-directed .NET global-tool inventory and vendor uninstall operations.

Installed global tools are useful software, not disposable cache. DevClean can
inventory them cheaply and let the user choose whether an individual tool is no
longer needed. Removal is delegated to ``dotnet tool uninstall --global`` so the
.NET SDK owns its internal ``.store`` layout and cleanup semantics.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import devclean.core.dotnet_maintenance as dotnet_maintenance
import devclean.core.nuget_cleanup as nuget_cleanup


RunFactory = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class DotnetGlobalTool:
    package_id: str
    version: str
    commands: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DotnetGlobalToolInventory:
    tools: tuple[DotnetGlobalTool, ...]
    storage_root: Path | None
    logical_bytes: int


@dataclass(frozen=True, slots=True)
class DotnetGlobalToolUninstallResult:
    tool: DotnetGlobalTool
    command: tuple[str, ...]
    before_bytes: int
    after_bytes: int
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def dotnet_global_tools_root(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Return Microsoft's documented default Windows global-tool directory."""

    env = _casefold_env(environment)
    userprofile = env.get("userprofile")
    if not userprofile:
        return None
    return Path(userprofile) / ".dotnet" / "tools"


def inventory_dotnet_global_tools(
    environment: Mapping[str, str] | None = None,
    *,
    runner: RunFactory = subprocess.run,
) -> DotnetGlobalToolInventory:
    """List installed user-global tools and total storage without mutation."""

    result = _run_dotnet(
        (nuget_cleanup.dotnet_executable(environment), "tool", "list", "--global"),
        environment,
        runner=runner,
        timeout=120,
    )
    if result.returncode != 0:
        output = _combined_output(result.stdout, result.stderr)
        raise RuntimeError(
            "dotnet tool list --global failed "
            f"(exit code {result.returncode}): {output or 'no output'}"
        )

    tools = _parse_global_tool_list(result.stdout)
    root = dotnet_global_tools_root(environment)
    logical_bytes = _directory_bytes(root) if root is not None else 0
    return DotnetGlobalToolInventory(
        tools=tools,
        storage_root=root,
        logical_bytes=logical_bytes,
    )


def uninstall_dotnet_global_tool(
    package_id: str,
    environment: Mapping[str, str] | None = None,
    *,
    runner: RunFactory = subprocess.run,
) -> DotnetGlobalToolUninstallResult:
    """Uninstall one currently listed global tool after explicit user choice."""

    if not isinstance(package_id, str) or not package_id.strip():
        raise ValueError("package_id must be a non-empty string")

    inventory = inventory_dotnet_global_tools(environment, runner=runner)
    requested = package_id.strip().casefold()
    matches = tuple(
        tool for tool in inventory.tools if tool.package_id.casefold() == requested
    )
    if len(matches) != 1:
        raise ValueError(f"not an installed .NET global tool: {package_id}")
    tool = matches[0]

    dotnet_maintenance.clear_dotnet_process_cache()
    if dotnet_maintenance.dotnet_sdk_process_running():
        raise RuntimeError(
            ".NET SDK, MSBuild, or Visual Studio is running; close it before uninstalling a tool"
        )

    before = _directory_bytes(inventory.storage_root)
    command = (
        nuget_cleanup.dotnet_executable(environment),
        "tool",
        "uninstall",
        "--global",
        tool.package_id,
    )
    result = _run_dotnet(command, environment, runner=runner, timeout=600)
    output = _combined_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            "dotnet tool uninstall --global failed "
            f"(exit code {result.returncode}): {output or 'no output'}"
        )
    after = _directory_bytes(inventory.storage_root)
    return DotnetGlobalToolUninstallResult(
        tool=tool,
        command=command,
        before_bytes=before,
        after_bytes=after,
        output=output,
    )


def _run_dotnet(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    runner: RunFactory,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        return runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"unable to execute .NET CLI: {error}") from error


def _parse_global_tool_list(stdout: str | None) -> tuple[DotnetGlobalTool, ...]:
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    separator = next(
        (
            index
            for index, line in enumerate(lines)
            if len(line) >= 3 and set(line) == {"-"}
        ),
        None,
    )
    if separator is None:
        raise RuntimeError("unable to parse dotnet tool list --global output")

    tools: list[DotnetGlobalTool] = []
    seen: set[str] = set()
    for line in lines[separator + 1 :]:
        fields = line.split()
        if len(fields) < 3:
            raise RuntimeError("unable to parse dotnet tool list --global row")
        package_id = fields[0]
        version = fields[1]
        commands = tuple(
            command.strip()
            for command in " ".join(fields[2:]).split(",")
            if command.strip()
        )
        if not commands:
            raise RuntimeError("dotnet global-tool row has no command")
        key = package_id.casefold()
        if key in seen:
            raise RuntimeError("dotnet tool list --global returned duplicate package ids")
        seen.add(key)
        tools.append(
            DotnetGlobalTool(
                package_id=package_id,
                version=version,
                commands=commands,
            )
        )
    return tuple(tools)


def _directory_bytes(root: Path | None) -> int:
    if root is None:
        return 0
    total = 0
    try:
        if not root.is_dir():
            return 0
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


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    return "\n".join(
        chunk.strip() for chunk in (stdout, stderr) if chunk and chunk.strip()
    )


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "DotnetGlobalTool",
    "DotnetGlobalToolInventory",
    "DotnetGlobalToolUninstallResult",
    "dotnet_global_tools_root",
    "inventory_dotnet_global_tools",
    "uninstall_dotnet_global_tool",
]
