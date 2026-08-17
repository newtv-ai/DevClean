r"""Audited NuGet local-storage semantics for Windows cleanup.

NuGet exposes its local package/cache locations and their supported clear
operations through ``dotnet nuget locals``. DevClean inventories those exact
locations but does not recursively delete them itself. In particular, projects
using PackageReference consume packages directly from ``global-packages``, so
that directory is vendor-managed dependency storage rather than a generic cache
folder that DevClean may erase by name.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
)


@dataclass(frozen=True, slots=True)
class NuGetRootSet:
    global_packages_roots: tuple[PureWindowsPath, ...]
    http_cache_roots: tuple[PureWindowsPath, ...]
    temp_roots: tuple[PureWindowsPath, ...]
    plugins_cache_roots: tuple[PureWindowsPath, ...]
    config_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    rebuild_cost: RebuildCost,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="nuget",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_NUGET_GLOBAL_PACKAGES_RULE = _rule(
    "nuget-global-packages-vendor-managed",
    root_key="NUGET_GLOBAL_PACKAGES",
    label="NuGet global packages dependency store; clear only through NuGet",
    rebuild_cost=RebuildCost.HIGH,
)
_NUGET_HTTP_CACHE_RULE = _rule(
    "nuget-http-cache-vendor-managed",
    root_key="NUGET_HTTP_CACHE",
    label="NuGet HTTP request cache; clear through NuGet locals",
    rebuild_cost=RebuildCost.LOW,
)
_NUGET_TEMP_RULE = _rule(
    "nuget-temp-vendor-managed",
    root_key="NUGET_TEMP",
    label="NuGet temporary cache; clear through NuGet locals",
    rebuild_cost=RebuildCost.NONE,
)
_NUGET_PLUGINS_CACHE_RULE = _rule(
    "nuget-plugins-cache-vendor-managed",
    root_key="NUGET_PLUGINS_CACHE",
    label="NuGet plugin operation-claims cache; clear through NuGet locals",
    rebuild_cost=RebuildCost.LOW,
)
_NUGET_CONFIG_RULE = _rule(
    "nuget-configuration",
    root_key="NUGET_CONFIG",
    label="NuGet package sources, credentials and restore configuration",
    rebuild_cost=RebuildCost.HIGH,
)
_NUGET_PROJECT_METADATA_RULE = _rule(
    "nuget-project-metadata",
    root_key="ANYWHERE",
    label="NuGet project dependency and restore metadata",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)

NUGET_RULES: tuple[ApplicationCleanupRule, ...] = (
    _NUGET_GLOBAL_PACKAGES_RULE,
    _NUGET_HTTP_CACHE_RULE,
    _NUGET_TEMP_RULE,
    _NUGET_PLUGINS_CACHE_RULE,
    _NUGET_CONFIG_RULE,
    _NUGET_PROJECT_METADATA_RULE,
)

_PROJECT_METADATA_NAMES = frozenset(
    {
        "nuget.config",
        "packages.config",
        "packages.lock.json",
        "directory.packages.props",
    }
)


def nuget_roots(environment: Mapping[str, str] | None = None) -> NuGetRootSet:
    env = _casefold_env(environment)
    discovered = _effective_nuget_locals() if environment is None else {}

    home = env.get("userprofile")
    local = env.get("localappdata")
    temp = env.get("temp") or env.get("tmp")
    appdata = env.get("appdata")

    global_packages = _resolve_root(
        env,
        discovered,
        env_keys=("devclean_nuget_packages", "nuget_packages"),
        local_key="global-packages",
        default=(PureWindowsPath(home) / ".nuget" / "packages" if home else None),
    )
    http_cache = _resolve_root(
        env,
        discovered,
        env_keys=("devclean_nuget_http_cache_path", "nuget_http_cache_path"),
        local_key="http-cache",
        default=(PureWindowsPath(local) / "NuGet" / "v3-cache" if local else None),
    )
    scratch = _resolve_root(
        env,
        discovered,
        env_keys=("devclean_nuget_scratch", "nuget_scratch"),
        local_key="temp",
        default=(PureWindowsPath(temp) / "NuGetScratch" if temp else None),
    )
    plugins_cache = _resolve_root(
        env,
        discovered,
        env_keys=(
            "devclean_nuget_plugins_cache_path",
            "nuget_plugins_cache_path",
        ),
        local_key="plugins-cache",
        default=(
            PureWindowsPath(local) / "NuGet" / "plugins-cache" if local else None
        ),
    )

    config_paths: list[PureWindowsPath] = []
    if appdata:
        config_paths.append(PureWindowsPath(appdata) / "NuGet" / "NuGet.Config")
    if home:
        config_paths.append(PureWindowsPath(home) / ".nuget" / "NuGet" / "NuGet.Config")

    return NuGetRootSet(
        global_packages_roots=_tuple_if_path(global_packages),
        http_cache_roots=_tuple_if_path(http_cache),
        temp_roots=_tuple_if_path(scratch),
        plugins_cache_roots=_tuple_if_path(plugins_cache),
        config_paths=_unique_paths(config_paths),
    )


def nuget_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = nuget_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.global_packages_roots,
                *roots.http_cache_roots,
                *roots.temp_roots,
                *roots.plugins_cache_roots,
            )
        )
    )


def match_nuget_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = nuget_roots(environment)
    groups = (
        (roots.global_packages_roots, _NUGET_GLOBAL_PACKAGES_RULE),
        (roots.http_cache_roots, _NUGET_HTTP_CACHE_RULE),
        (roots.temp_roots, _NUGET_TEMP_RULE),
        (roots.plugins_cache_roots, _NUGET_PLUGINS_CACHE_RULE),
        (roots.config_paths, _NUGET_CONFIG_RULE),
    )
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    for candidates, rule in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
                matches.append((len(normalized_root), rule))

    name = PureWindowsPath(str(path)).name.casefold()
    if name in _PROJECT_METADATA_NAMES:
        matches.append((len(normalized), _NUGET_PROJECT_METADATA_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_nuget_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_nuget_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = (
        None
        if observed is None
        else max(0.0, (current - observed).total_seconds() / 86_400)
    )
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def nuget_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_nuget_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def nuget_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:nuget|msbuild|devenv)\\.exe$' -or "
        "(($_.Name -match '(?i)^dotnet\\.exe$') -and "
        "$_.CommandLine -match '(?i)(?:^|\\s)(?:nuget|restore|build|test|publish|pack)(?:\\s|$)') "
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


@lru_cache(maxsize=1)
def _effective_nuget_locals() -> dict[str, str]:
    executable = dotnet_executable()
    try:
        result = subprocess.run(
            [
                executable,
                "nuget",
                "locals",
                "all",
                "--list",
                "--force-english-output",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    found: dict[str, str] = {}
    accepted = {"global-packages", "http-cache", "temp", "plugins-cache"}
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().casefold()
        candidate = value.strip().strip('"').strip("'")
        if key not in accepted or not candidate:
            continue
        path = PureWindowsPath(candidate)
        if path.is_absolute():
            found[key] = str(path)
    return found


def dotnet_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_dotnet_exe")
    if configured:
        return configured
    return "dotnet.exe" if os.name == "nt" else "dotnet"


def clear_nuget_process_cache() -> None:
    nuget_process_running.cache_clear()
    _effective_nuget_locals.cache_clear()


def _resolve_root(
    env: Mapping[str, str],
    discovered: Mapping[str, str],
    *,
    env_keys: tuple[str, ...],
    local_key: str,
    default: PureWindowsPath | None,
) -> PureWindowsPath | None:
    for key in env_keys:
        value = env.get(key)
        if not value:
            continue
        candidate = PureWindowsPath(value)
        if candidate.is_absolute():
            return candidate
    active = discovered.get(local_key)
    if active:
        candidate = PureWindowsPath(active)
        if candidate.is_absolute():
            return candidate
    return default


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = _impl._normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(path)
    return tuple(found)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "NUGET_RULES",
    "NuGetRootSet",
    "clear_nuget_process_cache",
    "dotnet_executable",
    "evaluate_nuget_path",
    "match_nuget_rule",
    "nuget_audited_tool_roots",
    "nuget_process_running",
    "nuget_roots",
    "nuget_scan_roots",
    "whole_tree_nuget_rule",
]
