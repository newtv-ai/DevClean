"""Vendor-owned pip cache maintenance inside one exact WSL distribution."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_exec import WslExecResult, run_wsl_exec
from devclean.core.wsl_inventory import WslDistribution, inspect_wsl
from devclean.core.wsl_path_scope import require_wsl_root_filesystem_path

_PIP_ENTRYPOINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python3", ("-m", "pip")),
    ("python", ("-m", "pip")),
    ("pip3", ()),
    ("pip", ()),
)
_PIP_COMM_RE = re.compile(r"^pip(?:3(?:\.\d+)*)?$")
_PYTHON_COMM_RE = re.compile(r"^python(?:3(?:\.\d+)*)?$")
_PYTHON_MODULE_PIP_RE = re.compile(r"(?:^|\s)-m\s+pip(?:\s|$)")
_PIP_SCRIPT_RE = re.compile(r"(?:^|\s)(?:\S*/)?pip(?:3(?:\.\d+)*)?(?:\s|$)")


@dataclass(frozen=True, slots=True)
class WslPipEntrypoint:
    executable: str
    prefix_arguments: tuple[str, ...]
    version_text: str

    @property
    def display(self) -> str:
        return " ".join((self.executable, *self.prefix_arguments))


@dataclass(frozen=True, slots=True)
class WslPipCacheInventory:
    distribution: str
    distribution_was_running: bool
    entrypoint: WslPipEntrypoint
    cache_path: str
    cache_info: str


@dataclass(frozen=True, slots=True)
class WslPipPurgeResult:
    before: WslPipCacheInventory
    after: WslPipCacheInventory
    command: tuple[str, ...]
    output: str


def inventory_wsl_pip_cache(
    distribution: str,
    environment: Mapping[str, str] | None = None,
) -> WslPipCacheInventory:
    """Ask pip itself for its effective cache identity inside one exact distro."""

    distro = _exact_distribution(distribution, environment)
    entrypoint, cache_path = _discover_pip(distro.name, environment)
    info = _run_pip(
        distro.name,
        entrypoint,
        ("cache", "info"),
        environment,
        timeout=60,
    )
    return WslPipCacheInventory(
        distribution=distro.name,
        distribution_was_running=distro.running,
        entrypoint=entrypoint,
        cache_path=cache_path,
        cache_info=info.stdout,
    )


def purge_wsl_pip_cache(
    expected: WslPipCacheInventory,
    environment: Mapping[str, str] | None = None,
) -> WslPipPurgeResult:
    """Purge only the cache freshly re-confirmed by the same pip entry point."""

    fresh = inventory_wsl_pip_cache(expected.distribution, environment)
    if _inventory_identity(fresh) != _inventory_identity(expected):
        raise RuntimeError("WSL pip identity/cache path changed before purge; please inspect again")

    _require_pip_idle(fresh.distribution, environment)
    require_wsl_root_filesystem_path(
        fresh.distribution,
        fresh.cache_path,
        environment,
    )
    result = _run_pip(
        fresh.distribution,
        fresh.entrypoint,
        ("cache", "purge"),
        environment,
        timeout=600,
    )

    after = inventory_wsl_pip_cache(fresh.distribution, environment)
    if _inventory_identity(after) != _inventory_identity(fresh):
        raise RuntimeError(
            "WSL pip identity/cache path changed after purge; result cannot be confirmed"
        )
    return WslPipPurgeResult(
        before=fresh,
        after=after,
        command=result.command,
        output=_combined_output(result.stdout, result.stderr),
    )


def _exact_distribution(
    requested: str,
    environment: Mapping[str, str] | None,
) -> WslDistribution:
    name = requested.strip()
    if not name or "\x00" in name:
        raise ValueError("WSL distribution name is invalid")
    matches = [
        distro
        for distro in inspect_wsl(environment).distributions
        if distro.name.casefold() == name.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Cannot uniquely identify WSL distribution {requested!r}: found={len(matches)}"
        )
    return matches[0]


def _discover_pip(
    distribution: str,
    environment: Mapping[str, str] | None,
) -> tuple[WslPipEntrypoint, str]:
    failures: list[str] = []
    for executable, prefix in _PIP_ENTRYPOINTS:
        try:
            cache_result = run_wsl_exec(
                distribution,
                executable,
                (*prefix, "cache", "dir"),
                environment,
                timeout=60,
            )
        except RuntimeError as error:
            failures.append(f"{executable}: {error}")
            continue

        cache_path = _validated_cache_path(cache_result.stdout)
        version = run_wsl_exec(
            distribution,
            executable,
            (*prefix, "--version"),
            environment,
            timeout=60,
        )
        version_text = _single_line(version.stdout, "pip --version")
        return WslPipEntrypoint(executable, prefix, version_text), cache_path

    detail = "; ".join(failures[-2:])
    raise RuntimeError(
        "No usable pip cache management entry point was found in the selected WSL distro"
        + (f": {detail}" if detail else "")
    )


def _run_pip(
    distribution: str,
    entrypoint: WslPipEntrypoint,
    arguments: Sequence[str],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> WslExecResult:
    return run_wsl_exec(
        distribution,
        entrypoint.executable,
        (*entrypoint.prefix_arguments, *arguments),
        environment,
        timeout=timeout,
    )


def _validated_cache_path(output: str) -> str:
    value = _single_line(output, "pip cache dir")
    if "\x00" in value:
        raise RuntimeError("pip cache dir returned a NUL-containing path")
    path = PurePosixPath(value)
    if not path.is_absolute() or value == "/":
        raise RuntimeError(f"pip cache dir returned an unsafe/non-absolute path: {value!r}")
    return str(path)


def _single_line(output: str, source: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"{source} did not return exactly one non-empty line")
    return lines[0]


def _require_pip_idle(
    distribution: str,
    environment: Mapping[str, str] | None,
) -> None:
    try:
        result = run_wsl_exec(
            distribution,
            "ps",
            ("-ww", "-eo", "comm=,args="),
            environment,
            timeout=30,
        )
    except RuntimeError as error:
        raise RuntimeError("Cannot confirm WSL pip process state; purge stopped safely") from error

    if any(_looks_like_pip_process(line) for line in result.stdout.splitlines()):
        raise RuntimeError(
            "pip appears to be running in this WSL distro; wait before purging cache"
        )


def _looks_like_pip_process(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    parts = stripped.split(maxsplit=1)
    comm = parts[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    args = parts[1].casefold() if len(parts) == 2 else ""
    if _PIP_COMM_RE.fullmatch(comm):
        return True
    if not _PYTHON_COMM_RE.fullmatch(comm):
        return False
    return bool(_PYTHON_MODULE_PIP_RE.search(args) or _PIP_SCRIPT_RE.search(args))


def _inventory_identity(
    inventory: WslPipCacheInventory,
) -> tuple[str, str, tuple[str, ...], str, str]:
    return (
        inventory.distribution.casefold(),
        inventory.entrypoint.executable,
        inventory.entrypoint.prefix_arguments,
        inventory.entrypoint.version_text,
        inventory.cache_path,
    )


def _combined_output(stdout: str, stderr: str) -> str:
    return "\n".join(chunk.strip() for chunk in (stdout, stderr) if chunk.strip())


__all__ = [
    "WslPipCacheInventory",
    "WslPipEntrypoint",
    "WslPipPurgeResult",
    "inventory_wsl_pip_cache",
    "purge_wsl_pip_cache",
]
