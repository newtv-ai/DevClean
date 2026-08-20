"""Vendor-owned uv cache pruning inside one exact WSL distribution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_exec import WslExecResult, run_wsl_exec
from devclean.core.wsl_inventory import WslDistribution, inspect_wsl
from devclean.core.wsl_path_scope import require_wsl_root_filesystem_path


@dataclass(frozen=True, slots=True)
class WslUvCacheInventory:
    distribution: str
    distribution_was_running: bool
    version_text: str
    cache_path: str


@dataclass(frozen=True, slots=True)
class WslUvPruneResult:
    before: WslUvCacheInventory
    after: WslUvCacheInventory
    command: tuple[str, ...]
    output: str


def inventory_wsl_uv_cache(
    distribution: str,
    environment: Mapping[str, str] | None = None,
) -> WslUvCacheInventory:
    """Ask uv itself for version and effective cache path in one exact distro."""

    distro = _exact_distribution(distribution, environment)
    version = run_wsl_exec(
        distro.name,
        "uv",
        ("--version",),
        environment,
        timeout=60,
    )
    cache = run_wsl_exec(
        distro.name,
        "uv",
        ("cache", "dir"),
        environment,
        timeout=60,
    )
    return WslUvCacheInventory(
        distribution=distro.name,
        distribution_was_running=distro.running,
        version_text=_single_line(version.stdout, "uv --version"),
        cache_path=_validated_cache_path(cache.stdout),
    )


def prune_wsl_uv_cache(
    expected: WslUvCacheInventory,
    environment: Mapping[str, str] | None = None,
) -> WslUvPruneResult:
    """Run uv's periodic safe prune for the freshly re-confirmed cache identity."""

    fresh = inventory_wsl_uv_cache(expected.distribution, environment)
    if _inventory_identity(fresh) != _inventory_identity(expected):
        raise RuntimeError("WSL uv identity/cache path changed before prune; please inspect again")

    require_wsl_root_filesystem_path(
        fresh.distribution,
        fresh.cache_path,
        environment,
    )
    result = run_wsl_exec(
        fresh.distribution,
        "uv",
        ("--cache-dir", fresh.cache_path, "cache", "prune"),
        environment,
        timeout=900,
    )

    after = inventory_wsl_uv_cache(fresh.distribution, environment)
    if _inventory_identity(after) != _inventory_identity(fresh):
        raise RuntimeError(
            "WSL uv identity/cache path changed after prune; result cannot be confirmed"
        )
    return WslUvPruneResult(
        before=fresh,
        after=after,
        command=result.command,
        output=_combined_output(result),
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


def _validated_cache_path(output: str) -> str:
    value = _single_line(output, "uv cache dir")
    if "\x00" in value:
        raise RuntimeError("uv cache dir returned a NUL-containing path")
    path = PurePosixPath(value)
    if not path.is_absolute() or value == "/":
        raise RuntimeError(f"uv cache dir returned an unsafe/non-absolute path: {value!r}")
    return str(path)


def _single_line(output: str, source: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"{source} did not return exactly one non-empty line")
    return lines[0]


def _inventory_identity(inventory: WslUvCacheInventory) -> tuple[str, str, str]:
    return (
        inventory.distribution.casefold(),
        inventory.version_text,
        inventory.cache_path,
    )


def _combined_output(result: WslExecResult) -> str:
    return "\n".join(chunk.strip() for chunk in (result.stdout, result.stderr) if chunk.strip())


__all__ = [
    "WslUvCacheInventory",
    "WslUvPruneResult",
    "inventory_wsl_uv_cache",
    "prune_wsl_uv_cache",
]
