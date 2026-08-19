"""Conservative Go build-cache maintenance inside one exact WSL distro."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from devclean.core.wsl_exec import (
    WslExecResult,
    run_wsl_exec,
    run_wsl_exec_with_env,
)
from devclean.core.wsl_inventory import WslDistribution, inspect_wsl
from devclean.core.wsl_path_scope import require_wsl_root_filesystem_path

_GO_COMM_RE = re.compile(r"^(?:go|gopls)$")


@dataclass(frozen=True, slots=True)
class WslGoBuildCacheInventory:
    distribution: str
    distribution_was_running: bool
    version_text: str
    cache_path: str
    cache_program: str

    @property
    def executable(self) -> bool:
        return not self.cache_program


@dataclass(frozen=True, slots=True)
class WslGoBuildCacheCleanResult:
    before: WslGoBuildCacheInventory
    after: WslGoBuildCacheInventory
    command: tuple[str, ...]
    output: str


def inventory_wsl_go_build_cache(
    distribution: str,
    environment: Mapping[str, str] | None = None,
) -> WslGoBuildCacheInventory:
    """Ask Go for its exact build-cache path and external-cache setting."""

    distro = _exact_distribution(distribution, environment)
    version = run_wsl_exec(
        distro.name,
        "go",
        ("version",),
        environment,
        timeout=60,
    )
    config = run_wsl_exec(
        distro.name,
        "go",
        ("env", "-json", "GOCACHE", "GOCACHEPROG"),
        environment,
        timeout=60,
    )
    cache_path, cache_program = _parse_go_env(config.stdout)
    return WslGoBuildCacheInventory(
        distribution=distro.name,
        distribution_was_running=distro.running,
        version_text=_single_line(version.stdout, "go version"),
        cache_path=cache_path,
        cache_program=cache_program,
    )


def clean_wsl_go_build_cache(
    expected: WslGoBuildCacheInventory,
    environment: Mapping[str, str] | None = None,
) -> WslGoBuildCacheCleanResult:
    """Run only ``go clean -cache`` for one freshly re-confirmed local cache."""

    fresh = inventory_wsl_go_build_cache(expected.distribution, environment)
    if _inventory_identity(fresh) != _inventory_identity(expected):
        raise RuntimeError(
            "WSL Go identity/build-cache configuration changed before clean; inspect again"
        )
    if fresh.cache_program:
        raise RuntimeError(
            "GOCACHEPROG is configured; external Go cache backends remain report-only"
        )

    _require_go_idle(fresh.distribution, environment)
    require_wsl_root_filesystem_path(
        fresh.distribution,
        fresh.cache_path,
        environment,
    )
    result = run_wsl_exec_with_env(
        fresh.distribution,
        "go",
        ("clean", "-cache"),
        {
            "GOCACHE": fresh.cache_path,
            "GOCACHEPROG": "",
        },
        environment,
        timeout=900,
    )

    after = inventory_wsl_go_build_cache(fresh.distribution, environment)
    if _inventory_identity(after) != _inventory_identity(fresh):
        raise RuntimeError(
            "WSL Go identity/build-cache configuration changed after clean; "
            "result cannot be confirmed"
        )
    return WslGoBuildCacheCleanResult(
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


def _parse_go_env(output: str) -> tuple[str, str]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise RuntimeError("go env -json returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("go env -json did not return an object")
    cache = payload.get("GOCACHE")
    cache_program = payload.get("GOCACHEPROG")
    if not isinstance(cache, str) or not isinstance(cache_program, str):
        raise RuntimeError("go env -json did not return string GOCACHE/GOCACHEPROG values")
    return _validated_cache_path(cache), cache_program.strip()


def _validated_cache_path(value: str) -> str:
    if not value or "\x00" in value:
        raise RuntimeError("go env GOCACHE returned an invalid path")
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) == "/":
        raise RuntimeError(f"go env GOCACHE returned an unsafe/non-absolute path: {value!r}")
    return str(path)


def _single_line(output: str, source: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"{source} did not return exactly one non-empty line")
    return lines[0]


def _require_go_idle(
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
        raise RuntimeError(
            "Cannot confirm WSL Go/gopls process state; clean stopped safely"
        ) from error
    if any(_looks_like_go_process(line) for line in result.stdout.splitlines()):
        raise RuntimeError(
            "Go or gopls appears to be running in this WSL distro; wait before cleaning cache"
        )


def _looks_like_go_process(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    comm = stripped.split(maxsplit=1)[0]
    base = comm.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return bool(_GO_COMM_RE.fullmatch(base))


def _inventory_identity(
    inventory: WslGoBuildCacheInventory,
) -> tuple[str, str, str, str]:
    return (
        inventory.distribution.casefold(),
        inventory.version_text,
        inventory.cache_path,
        inventory.cache_program,
    )


def _combined_output(result: WslExecResult) -> str:
    return "\n".join(
        chunk.strip() for chunk in (result.stdout, result.stderr) if chunk.strip()
    )


__all__ = [
    "WslGoBuildCacheCleanResult",
    "WslGoBuildCacheInventory",
    "clean_wsl_go_build_cache",
    "inventory_wsl_go_build_cache",
]
