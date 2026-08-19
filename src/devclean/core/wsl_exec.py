"""Internal non-shell execution boundary for audited tools inside one WSL distro."""

from __future__ import annotations

import locale
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_inventory import (
    WslDistribution,
    inspect_wsl,
    wsl_executable,
)


_FORBIDDEN_EXECUTABLES = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "fish",
        "csh",
        "tcsh",
        "sudo",
        "su",
        "doas",
        "rm",
        "rmdir",
        "unlink",
        "find",
        "xargs",
        "dd",
        "truncate",
        "shred",
        "mount",
        "umount",
        "fdisk",
        "sfdisk",
        "parted",
        "mkfs",
        "systemctl",
        "service",
        "apt",
        "apt-get",
        "dnf",
        "yum",
        "pacman",
        "zypper",
        "apk",
        "docker",
        "podman",
        "node",
        "perl",
        "ruby",
        "php",
        "lua",
        "pwsh",
        "powershell",
        "powershell.exe",
        "cmd",
        "cmd.exe",
        "wsl",
        "wsl.exe",
    }
)
_PYTHON_NAMES = frozenset({"python", "python3"})
_ALLOWED_PYTHON_MODULES = frozenset({"pip"})


@dataclass(frozen=True, slots=True)
class WslExecResult:
    distribution: str
    executable: str
    arguments: tuple[str, ...]
    command: tuple[str, ...]
    stdout: str
    stderr: str


def run_wsl_exec(
    distribution: str,
    executable: str,
    arguments: Sequence[str] = (),
    environment: Mapping[str, str] | None = None,
    *,
    timeout: int = 120,
) -> WslExecResult:
    """Run one code-supplied argv command in one exact registered distribution.

    This helper deliberately has no shell mode and grants no standalone cleanup
    authority. Callers remain responsible for the vendor-specific inventory,
    process guards, path/config checks, and mutation revalidation.
    """

    if timeout <= 0:
        raise ValueError("WSL command timeout 必须大于 0")
    distro = _require_distribution(distribution, environment)
    tool = _validate_executable(executable)
    argv = _validate_arguments(tool, arguments)
    wsl = wsl_executable(environment)
    command = (
        wsl,
        "--distribution",
        distro.name,
        "--exec",
        executable,
        *argv,
    )

    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=False,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 WSL tool command: {error}") from error

    stdout = _decode_output(completed.stdout)
    stderr = _decode_output(completed.stderr)
    if completed.returncode != 0:
        detail = (stderr or stdout).strip()
        raise RuntimeError(
            f"WSL tool command 失败 (exit {completed.returncode})"
            + (f": {detail}" if detail else "")
        )

    after = _require_distribution(distro.name, environment)
    if after.name.casefold() != distro.name.casefold():
        raise RuntimeError("WSL distribution identity 在执行后发生变化; 无法确认结果")

    return WslExecResult(
        distribution=distro.name,
        executable=executable,
        arguments=argv,
        command=command,
        stdout=stdout.strip(),
        stderr=stderr.strip(),
    )


def _require_distribution(
    requested: str,
    environment: Mapping[str, str] | None,
) -> WslDistribution:
    name = requested.strip()
    if not name or "\x00" in name:
        raise ValueError("WSL distribution name 无效")
    inventory = inspect_wsl(environment)
    matches = [
        distro
        for distro in inventory.distributions
        if distro.name.casefold() == name.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"无法唯一确认 WSL distribution {requested!r}: found={len(matches)}"
        )
    return matches[0]


def _validate_executable(executable: str) -> str:
    value = executable.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("WSL tool executable 无效")
    base = PurePosixPath(value.replace("\\", "/")).name.casefold()
    if not base:
        raise ValueError("WSL tool executable 无效")
    if base in _FORBIDDEN_EXECUTABLES or base.startswith("mkfs."):
        raise ValueError(f"WSL execution boundary 禁止直接执行 {base!r}")
    return base


def _validate_arguments(tool: str, arguments: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in arguments:
        value = str(raw)
        if "\x00" in value:
            raise ValueError("WSL tool argument 包含 NUL")
        result.append(value)
    argv = tuple(result)

    python_family = tool in _PYTHON_NAMES or (
        tool.startswith("python3.")
        and tool.removeprefix("python3.").replace(".", "").isdigit()
    )
    if python_family:
        if len(argv) < 2 or argv[0] != "-m" or argv[1] not in _ALLOWED_PYTHON_MODULES:
            raise ValueError("WSL Python execution 目前只允许 code-defined `python -m pip` 入口")
        if any(argument == "-c" or argument.startswith("-c") for argument in argv):
            raise ValueError("WSL execution boundary 禁止 Python -c 动态代码")
    return argv


def _decode_output(output: bytes) -> str:
    if not output:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return output.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return output.decode("utf-8", errors="replace")


__all__ = ["WslExecResult", "run_wsl_exec"]
