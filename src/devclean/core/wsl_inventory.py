"""Read-only WSL distribution inventory with no VHD path guessing."""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WslDistribution:
    name: str
    running: bool


@dataclass(frozen=True, slots=True)
class WslInventory:
    executable: str
    version_text: str
    status_text: str
    distributions: tuple[WslDistribution, ...]


def inspect_wsl(
    environment: Mapping[str, str] | None = None,
) -> WslInventory:
    """Ask WSL itself for registered distributions and running state."""

    executable = wsl_executable(environment)
    all_result = _run_wsl(
        executable,
        ("--list", "--quiet"),
        environment,
        timeout=30,
    )
    names = _distribution_names(all_result.stdout)

    running_result = _run_wsl_allow_status(
        executable,
        ("--list", "--running", "--quiet"),
        environment,
        timeout=30,
    )
    if running_result.returncode != 0:
        detail = _decoded_error(running_result)
        raise RuntimeError(
            "WSL 无法确认 running distributions"
            + (f": {detail}" if detail else "")
        )
    running_names = {
        name.casefold() for name in _distribution_names(running_result.stdout)
    }

    version_result = _run_wsl_allow_status(
        executable,
        ("--version",),
        environment,
        timeout=30,
    )
    version_text = (
        _decode_output(version_result.stdout).strip()
        if version_result.returncode == 0
        else ""
    )

    status_result = _run_wsl_allow_status(
        executable,
        ("--status",),
        environment,
        timeout=30,
    )
    status_text = (
        _decode_output(status_result.stdout).strip()
        if status_result.returncode == 0
        else ""
    )

    distributions = tuple(
        WslDistribution(name=name, running=name.casefold() in running_names)
        for name in names
    )
    return WslInventory(
        executable=executable,
        version_text=version_text,
        status_text=status_text,
        distributions=distributions,
    )


def wsl_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_wsl_exe")
    if configured:
        return configured
    if environment is None:
        located = shutil.which("wsl.exe" if os.name == "nt" else "wsl")
        if located:
            return located
    return "wsl.exe" if os.name == "nt" else "wsl"


def _distribution_names(output: bytes) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for raw in _decode_output(output).splitlines():
        name = raw.strip().lstrip("\ufeff").replace("\x00", "").strip()
        if name.startswith("*"):
            name = name[1:].strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(name)
    return tuple(values)


def _decode_output(output: bytes) -> str:
    if not output:
        return ""
    if output.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return output.decode("utf-16")
        except UnicodeDecodeError:
            pass
    sample = output[:200]
    if sample and sample.count(b"\x00") >= max(2, len(sample) // 6):
        try:
            return output.decode("utf-16-le")
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return output.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return output.decode("utf-8", errors="replace")


def _decoded_error(result: subprocess.CompletedProcess[bytes]) -> str:
    return (_decode_output(result.stderr) or _decode_output(result.stdout)).strip()


def _run_wsl(
    executable: str,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    result = _run_wsl_allow_status(
        executable,
        arguments,
        environment,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = _decoded_error(result)
        raise RuntimeError(
            f"WSL CLI 失败 (exit {result.returncode})"
            + (f": {detail}" if detail else "")
        )
    return result


def _run_wsl_allow_status(
    executable: str,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        return subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=False,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 WSL CLI: {error}") from error


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items() if value}


__all__ = [
    "WslDistribution",
    "WslInventory",
    "inspect_wsl",
    "wsl_executable",
]
