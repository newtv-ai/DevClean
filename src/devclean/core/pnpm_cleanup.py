"""Audited pnpm storage semantics for Windows cleanup.

pnpm separates registry/dlx caches, state, its content-addressable store,
global installations, and PNPM_HOME. Cache subtrees are regenerable, but the
store may back active projects, so raw whole-tree store deletion is forbidden.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    effective_idle_days,
)
from devclean.platform.windows.volumes import fixed_volume_roots

_MIB = 1024**2
_DLX_IDLE_DAYS = 1.0


@dataclass(frozen=True, slots=True)
class PnpmRootSet:
    cache_roots: tuple[PureWindowsPath, ...]
    state_roots: tuple[PureWindowsPath, ...]
    home_roots: tuple[PureWindowsPath, ...]
    store_roots: tuple[PureWindowsPath, ...]
    global_roots: tuple[PureWindowsPath, ...]
    global_bin_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_kind: str,
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="pnpm",
        root_key=f"PNPM_{root_kind.upper()}",
        relative_pattern=relative_pattern,
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


PNPM_RULES: tuple[ApplicationCleanupRule, ...] = (
    _rule(
        "pnpm-dlx-cache",
        "dlx",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        RebuildCost.MEDIUM,
        "pnpm dlx temporary executable environments",
        root_kind="cache",
        idle_days=_DLX_IDLE_DAYS,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "pnpm-metadata-cache",
        "metadata-v*",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        RebuildCost.LOW,
        "pnpm registry metadata cache",
        root_kind="cache",
        idle_days=14,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
    ),
    _rule(
        "pnpm-cache-unclassified",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified pnpm cache-root state",
        root_kind="cache",
    ),
    _rule(
        "pnpm-update-state",
        "pnpm-state.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "pnpm update-check state",
        root_kind="state",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "pnpm-state-unclassified",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified pnpm state directory",
        root_kind="state",
    ),
    _rule(
        "pnpm-store",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "pnpm store; maintain with pnpm store prune instead of raw deletion",
        root_kind="store",
    ),
    _rule(
        "pnpm-global-install",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "pnpm globally installed packages",
        root_kind="global",
    ),
    _rule(
        "pnpm-global-bin",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "pnpm global executable shims",
        root_kind="global_bin",
    ),
    _rule(
        "pnpm-home",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "PNPM_HOME executables, configuration and persistent package-manager data",
        root_kind="home",
    ),
)

_PNPM_LOCK_RULE = ApplicationCleanupRule(
    rule_id="pnpm-project-metadata",
    app_id="pnpm",
    root_key="ANYWHERE",
    relative_pattern="",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="pnpm lock/workspace metadata",
)
_PNPM_METADATA_FILENAMES = frozenset({"pnpm-lock.yaml", "pnpm-workspace.yaml"})


def pnpm_roots(environment: Mapping[str, str] | None = None) -> PnpmRootSet:
    env = _casefold_env(environment)
    localappdata = env.get("localappdata")
    default_home = (
        PureWindowsPath(env["pnpm_home"])
        if env.get("pnpm_home")
        else PureWindowsPath(localappdata) / "pnpm"
        if localappdata
        else None
    )
    default_cache = (
        PureWindowsPath(localappdata) / "pnpm-cache" if localappdata else None
    )
    default_state = (
        PureWindowsPath(localappdata) / "pnpm-state" if localappdata else None
    )
    effective = _effective_pnpm_config() if environment is None else {}

    cache_value = _first_config_path(env, effective, "cache_dir", "cacheDir")
    state_value = _first_config_path(env, effective, "state_dir", "stateDir")
    store_value = _first_config_path(env, effective, "store_dir", "storeDir")
    global_value = _first_config_path(env, effective, "global_dir", "globalDir")
    global_bin_value = _first_config_path(
        env,
        effective,
        "global_bin_dir",
        "globalBinDir",
    )

    caches: list[PureWindowsPath] = []
    states: list[PureWindowsPath] = []
    homes: list[PureWindowsPath] = []
    stores: list[PureWindowsPath] = []
    globals_: list[PureWindowsPath] = []
    global_bins: list[PureWindowsPath] = []

    if cache_value:
        caches.append(PureWindowsPath(cache_value))
    if default_cache is not None:
        caches.append(default_cache)
    if state_value:
        states.append(PureWindowsPath(state_value))
    if default_state is not None:
        states.append(default_state)

    if default_home is not None:
        homes.append(default_home)
        if not global_value:
            globals_.append(default_home / "global")
        if not global_bin_value:
            global_bins.append(default_home / "bin")
        if not store_value:
            stores.append(default_home / "store")
    if global_value:
        globals_.append(PureWindowsPath(global_value))
    if global_bin_value:
        global_bins.append(PureWindowsPath(global_bin_value))
    if store_value:
        stores.append(PureWindowsPath(store_value))

    if environment is None:
        active_store = _active_pnpm_store_path()
        if active_store:
            stores.append(PureWindowsPath(active_store))
        for volume in fixed_volume_roots():
            candidate = PureWindowsPath(str(volume)) / ".pnpm-store"
            if _path_is_directory(candidate):
                stores.append(candidate)

    return PnpmRootSet(
        cache_roots=_unique_paths(caches),
        state_roots=_unique_paths(states),
        home_roots=_unique_paths(homes),
        store_roots=_unique_paths(stores),
        global_roots=_unique_paths(globals_),
        global_bin_roots=_unique_paths(global_bins),
    )


def pnpm_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = pnpm_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.cache_roots,
                *roots.state_roots,
                *roots.store_roots,
                *roots.global_roots,
                *roots.global_bin_roots,
                *roots.home_roots,
            )
        )
    )


def match_pnpm_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = pnpm_roots(environment)
    groups = {
        "PNPM_CACHE": roots.cache_roots,
        "PNPM_STATE": roots.state_roots,
        "PNPM_STORE": roots.store_roots,
        "PNPM_GLOBAL": roots.global_roots,
        "PNPM_GLOBAL_BIN": roots.global_bin_roots,
        "PNPM_HOME": roots.home_roots,
    }
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(PNPM_RULES):
        for root in groups.get(rule.root_key, ()):
            normalized_root = _impl._normalize(root)
            for expanded in _impl._expand_braces(rule.relative_pattern):
                candidate = normalized_root + ("\\" + expanded if expanded else "")
                if not _impl._matches(normalized, candidate, rule.match_kind):
                    continue
                if rule.owner is DecisionOwner.KEEP:
                    owner_weight = 3
                elif rule.owner is DecisionOwner.USER:
                    owner_weight = 2
                else:
                    owner_weight = 1
                matches.append((len(candidate), owner_weight * 1000 - index, rule))
    if matches:
        return max(matches, key=lambda item: (item[0], item[1]))[2]
    if PureWindowsPath(str(path)).name.casefold() in _PNPM_METADATA_FILENAMES:
        return _PNPM_LOCK_RULE
    return None


def pnpm_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = pnpm_roots(environment)
    dlx_rule = next(
        rule for rule in PNPM_RULES if rule.rule_id == "pnpm-dlx-cache"
    )
    metadata_rule = next(
        rule for rule in PNPM_RULES if rule.rule_id == "pnpm-metadata-cache"
    )
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for cache in roots.cache_roots:
        _append_tool_root(found, seen, cache / "dlx", dlx_rule)
        for child in _metadata_cache_dirs(cache):
            _append_tool_root(found, seen, child, metadata_rule)
    return tuple(found)


def whole_tree_pnpm_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in pnpm_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_pnpm_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_pnpm_rule(path, environment)
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
    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.KEEP_PROTECTED,
            observed,
            idle,
            None,
            0,
        )
    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = pnpm_process_running()
    score = _impl._benefit_score(logical_size, idle, threshold, rule.rebuild_cost)
    if rule.requires_process_closed and running:
        action = PolicyAction.TOOL_KEEP_IN_USE
    elif logical_size < rule.min_reclaim_bytes:
        action = PolicyAction.TOOL_KEEP_LOW_BENEFIT
    elif idle is None or threshold is None:
        action = PolicyAction.TOOL_KEEP_UNKNOWN_USAGE
    elif idle < threshold:
        action = PolicyAction.TOOL_KEEP_RECENT
    else:
        action = PolicyAction.TOOL_DELETE
    return ApplicationPolicyDecision(rule, action, observed, idle, threshold, score)


@lru_cache(maxsize=1)
def pnpm_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'node.exe' -and $_.CommandLine -match '(?i)pnpm' }; "
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
def _effective_pnpm_config() -> dict[str, str]:
    executable = "pnpm.cmd" if os.name == "nt" else "pnpm"
    try:
        result = subprocess.run(
            [executable, "config", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        raw = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    allowed = {"cacheDir", "stateDir", "storeDir", "globalDir", "globalBinDir"}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if key in allowed and isinstance(value, str) and value
    }


@lru_cache(maxsize=1)
def _active_pnpm_store_path() -> str | None:
    executable = "pnpm.cmd" if os.name == "nt" else "pnpm"
    try:
        result = subprocess.run(
            [executable, "store", "path", "--silent"],
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
    return lines[-1] if lines else None


def clear_pnpm_process_cache() -> None:
    pnpm_process_running.cache_clear()
    _effective_pnpm_config.cache_clear()
    _active_pnpm_store_path.cache_clear()


def _first_config_path(
    environment: Mapping[str, str],
    effective: Mapping[str, str],
    env_suffix: str,
    config_key: str,
) -> str | None:
    return (
        environment.get(f"pnpm_config_{env_suffix}")
        or environment.get(f"npm_config_{env_suffix}")
        or effective.get(config_key)
    )


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold().rstrip("\\/")
        if key and key not in seen:
            seen.add(key)
            found.append(path)
    return tuple(found)


def _metadata_cache_dirs(cache_root: PureWindowsPath) -> tuple[PureWindowsPath, ...]:
    try:
        children = tuple(Path(str(cache_root)).glob("metadata-v*"))
    except OSError:
        return ()
    return tuple(
        PureWindowsPath(str(child)) for child in children if child.is_dir()
    )


def _append_tool_root(
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]],
    seen: set[str],
    path: PureWindowsPath,
    rule: ApplicationCleanupRule,
) -> None:
    key = _impl._normalize(path)
    if key not in seen:
        seen.add(key)
        found.append((path, rule))


def _path_is_directory(path: PureWindowsPath) -> bool:
    try:
        return Path(str(path)).is_dir()
    except OSError:
        return False


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "PNPM_RULES",
    "PnpmRootSet",
    "clear_pnpm_process_cache",
    "evaluate_pnpm_path",
    "match_pnpm_rule",
    "pnpm_audited_tool_roots",
    "pnpm_process_running",
    "pnpm_roots",
    "pnpm_scan_roots",
    "whole_tree_pnpm_rule",
]
