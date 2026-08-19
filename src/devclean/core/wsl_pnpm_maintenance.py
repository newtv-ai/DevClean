"""Vendor-owned pnpm store garbage collection inside one exact WSL distro."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_exec import WslExecResult, run_wsl_exec
from devclean.core.wsl_inventory import WslDistribution, inspect_wsl

_PNPM_COMM_RE = re.compile(r"^pnpm(?:\.c?js)?$")
_NODE_COMM_RE = re.compile(r"^(?:node|nodejs)$")
_PNPM_ARG_RE = re.compile(r"(?:^|\s)(?:\S*/)?pnpm(?:\.c?js)?(?:\s|$)")


@dataclass(frozen=True, slots=True)
class WslPnpmStoreInventory:
    distribution: str
    distribution_was_running: bool
    version_text: str
    active_store_path: str
    store_dir: str


@dataclass(frozen=True, slots=True)
class WslPnpmPruneResult:
    before: WslPnpmStoreInventory
    after: WslPnpmStoreInventory
    command: tuple[str, ...]
    output: str


def inventory_wsl_pnpm_store(
    distribution: str,
    environment: Mapping[str, str] | None = None,
) -> WslPnpmStoreInventory:
    """Ask pnpm itself for its version and currently active store path."""

    distro = _exact_distribution(distribution, environment)
    version = run_wsl_exec(
        distro.name,
        "pnpm",
        ("--version",),
        environment,
        timeout=60,
    )
    active = run_wsl_exec(
        distro.name,
        "pnpm",
        ("store", "path", "--silent"),
        environment,
        timeout=60,
    )
    active_path = _validated_store_path(active.stdout, "pnpm store path")
    return WslPnpmStoreInventory(
        distribution=distro.name,
        distribution_was_running=distro.running,
        version_text=_single_line(version.stdout, "pnpm --version"),
        active_store_path=active_path,
        store_dir=_store_config_root(active_path),
    )


def prune_wsl_pnpm_store(
    expected: WslPnpmStoreInventory,
    environment: Mapping[str, str] | None = None,
) -> WslPnpmPruneResult:
    """Run pnpm's own GC for the freshly verified store identity."""

    fresh = inventory_wsl_pnpm_store(expected.distribution, environment)
    if _inventory_identity(fresh) != _inventory_identity(expected):
        raise RuntimeError("WSL pnpm identity/store changed before prune; please inspect again")

    _require_pnpm_idle(fresh.distribution, environment)
    scoped = run_wsl_exec(
        fresh.distribution,
        "pnpm",
        ("--store-dir", fresh.store_dir, "store", "path", "--silent"),
        environment,
        timeout=60,
    )
    scoped_path = _validated_store_path(scoped.stdout, "scoped pnpm store path")
    if scoped_path != fresh.active_store_path:
        raise RuntimeError("pnpm did not confirm the exact selected store path; prune stopped safely")

    result = run_wsl_exec(
        fresh.distribution,
        "pnpm",
        ("--store-dir", fresh.store_dir, "store", "prune"),
        environment,
        timeout=900,
    )

    after = inventory_wsl_pnpm_store(fresh.distribution, environment)
    if _inventory_identity(after) != _inventory_identity(fresh):
        raise RuntimeError(
            "WSL pnpm identity/store changed after prune; result cannot be confirmed"
        )
    return WslPnpmPruneResult(
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


def _validated_store_path(output: str, source: str) -> str:
    value = _single_line(output, source)
    if "\x00" in value:
        raise RuntimeError(f"{source} returned a NUL-containing path")
    path = PurePosixPath(value)
    if not path.is_absolute() or value == "/":
        raise RuntimeError(f"{source} returned an unsafe/non-absolute path: {value!r}")
    return str(path)


def _store_config_root(active_store_path: str) -> str:
    path = PurePosixPath(active_store_path)
    name = path.name.casefold()
    if len(name) > 1 and name.startswith("v") and name[1:].isdigit():
        path = path.parent
    if str(path) == "/":
        raise RuntimeError("pnpm store-dir resolved to filesystem root")
    return str(path)


def _single_line(output: str, source: str) -> str:
    lines = [line.strip().strip('"').strip("'") for line in output.splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0]:
        raise RuntimeError(f"{source} did not return exactly one non-empty line")
    return lines[0]


def _require_pnpm_idle(
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
        raise RuntimeError("Cannot confirm WSL pnpm process state; prune stopped safely") from error

    if any(_looks_like_pnpm_process(line) for line in result.stdout.splitlines()):
        raise RuntimeError("pnpm appears to be running in this WSL distro; wait before pruning store")


def _looks_like_pnpm_process(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    parts = stripped.split(maxsplit=1)
    comm = parts[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    args = parts[1].casefold() if len(parts) == 2 else ""
    if _PNPM_COMM_RE.fullmatch(comm):
        return True
    return bool(_NODE_COMM_RE.fullmatch(comm) and _PNPM_ARG_RE.search(args))


def _inventory_identity(
    inventory: WslPnpmStoreInventory,
) -> tuple[str, str, str, str]:
    return (
        inventory.distribution.casefold(),
        inventory.version_text,
        inventory.active_store_path,
        inventory.store_dir,
    )


def _combined_output(result: WslExecResult) -> str:
    return "\n".join(
        chunk.strip() for chunk in (result.stdout, result.stderr) if chunk.strip()
    )


__all__ = [
    "WslPnpmPruneResult",
    "WslPnpmStoreInventory",
    "inventory_wsl_pnpm_store",
    "prune_wsl_pnpm_store",
]
