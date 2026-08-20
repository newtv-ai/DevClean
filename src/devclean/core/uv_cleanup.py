r"""Audited uv storage semantics for Windows cleanup.

Astral documents uv's cache directory as disposable, but explicitly says it is
never safe to modify the cache directly and provides ``uv cache clean`` /
``uv cache prune`` for cache mutation. DevClean therefore inventories the cache
but grants it no generic file or whole-tree deletion authority. Persistent data,
configuration, managed Python installations and installed tools are KEEP.
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
class UvRootSet:
    cache_roots: tuple[PureWindowsPath, ...]
    data_roots: tuple[PureWindowsPath, ...]
    user_config_roots: tuple[PureWindowsPath, ...]
    system_config_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    rebuild_cost: RebuildCost,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="uv",
        root_key=root_key,
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_UV_CACHE_RULE = _rule(
    "uv-cache-vendor-managed",
    root_key="UV_CACHE",
    label="uv disposable cache; maintain with uv cache prune instead of raw deletion",
    rebuild_cost=RebuildCost.MEDIUM,
)
_UV_DATA_RULE = _rule(
    "uv-persistent-data",
    root_key="UV_DATA",
    label="uv persistent non-disposable data, managed Python versions and tools",
    rebuild_cost=RebuildCost.HIGH,
)
_UV_USER_CONFIG_RULE = _rule(
    "uv-user-config",
    root_key="UV_USER_CONFIG",
    label="uv user configuration",
    rebuild_cost=RebuildCost.HIGH,
)
_UV_SYSTEM_CONFIG_RULE = _rule(
    "uv-system-config",
    root_key="UV_SYSTEM_CONFIG",
    label="uv system configuration",
    rebuild_cost=RebuildCost.HIGH,
)

UV_RULES: tuple[ApplicationCleanupRule, ...] = (
    _UV_CACHE_RULE,
    _UV_DATA_RULE,
    _UV_USER_CONFIG_RULE,
    _UV_SYSTEM_CONFIG_RULE,
)


def uv_roots(environment: Mapping[str, str] | None = None) -> UvRootSet:
    env = _casefold_env(environment)
    cache_roots: list[PureWindowsPath] = []
    data_roots: list[PureWindowsPath] = []
    user_config_roots: list[PureWindowsPath] = []
    system_config_roots: list[PureWindowsPath] = []

    explicit_cache = env.get("devclean_uv_cache_dir") or env.get("uv_cache_dir")
    if explicit_cache:
        _append_absolute(cache_roots, explicit_cache)
    elif environment is None:
        active = _active_uv_cache_dir()
        if active:
            _append_absolute(cache_roots, active)
    if not cache_roots:
        localappdata = env.get("localappdata")
        if localappdata:
            cache_roots.append(PureWindowsPath(localappdata) / "uv" / "cache")

    appdata = env.get("appdata")
    if appdata:
        uv_config = PureWindowsPath(appdata) / "uv"
        user_config_roots.append(uv_config)
        data_roots.append(uv_config / "data")

    programdata = env.get("programdata")
    if programdata:
        system_config_roots.append(PureWindowsPath(programdata) / "uv")

    return UvRootSet(
        cache_roots=_unique_paths(cache_roots),
        data_roots=_unique_paths(data_roots),
        user_config_roots=_unique_paths(user_config_roots),
        system_config_roots=_unique_paths(system_config_roots),
    )


def uv_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = uv_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.cache_roots,
                *roots.data_roots,
                *roots.user_config_roots,
                *roots.system_config_roots,
            )
        )
    )


def match_uv_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = uv_roots(environment)
    groups = (
        (roots.cache_roots, _UV_CACHE_RULE),
        (roots.data_roots, _UV_DATA_RULE),
        (roots.user_config_roots, _UV_USER_CONFIG_RULE),
        (roots.system_config_roots, _UV_SYSTEM_CONFIG_RULE),
    )
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    for candidates, rule in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
                matches.append((len(normalized_root), rule))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_uv_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_uv_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def uv_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_uv_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def uv_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:uv|uvx)\\.exe$' -or "
        "(($_.Name -match '(?i)^(?:python|pythonw|py)(?:\\d+(?:\\.\\d+)?)?\\.exe$') "
        "-and $_.CommandLine -match '(?i)(?:^|\\s)-m\\s+uv(?:\\s|$)') }; "
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


@lru_cache(maxsize=1)
def _active_uv_cache_dir() -> str | None:
    executable = "uv.exe" if os.name == "nt" else "uv"
    try:
        result = subprocess.run(
            [executable, "cache", "dir"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    candidate = lines[-1].strip().strip('"').strip("'")
    return candidate if PureWindowsPath(candidate).is_absolute() else None


def clear_uv_process_cache() -> None:
    uv_process_running.cache_clear()
    _active_uv_cache_dir.cache_clear()


def _append_absolute(found: list[PureWindowsPath], value: str) -> None:
    candidate = PureWindowsPath(value)
    if candidate.is_absolute():
        found.append(candidate)


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
    "UV_RULES",
    "UvRootSet",
    "clear_uv_process_cache",
    "evaluate_uv_path",
    "match_uv_rule",
    "uv_audited_tool_roots",
    "uv_process_running",
    "uv_roots",
    "uv_scan_roots",
    "whole_tree_uv_rule",
]
