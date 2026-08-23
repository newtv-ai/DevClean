r"""Audited pnpm storage semantics for Windows cleanup.

Current pnpm owns several distinct storage lifecycles. Its store and DLX cache
have vendor cleanup operations, metadata mirrors can be intentionally useful for
offline resolution, and global installations are persistent payload. DevClean
therefore grants no raw whole-tree authority to those roots. The exact
``pnpm-state.json`` update-check throttle is separately source-backed as
regenerable TOOL state.
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
)
from devclean.platform.windows.volumes import fixed_volume_roots


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
    user_age_buckets: tuple[int, ...] = (),
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
        user_age_buckets=user_age_buckets,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


_PNPM_DLX_RULE = _rule(
    "pnpm-dlx-cache",
    "dlx",
    MatchKind.PREFIX,
    DecisionOwner.USER,
    RebuildCost.MEDIUM,
    "pnpm dlx temporary environments; expiry is managed by pnpm store prune",
    root_kind="cache",
    requires_process_closed=True,
    user_age_buckets=(7, 30, 90),
)

# Matching is performed by _is_metadata_cache_path rather than by this sentinel
# pattern. Current pnpm uses vN/metadata* while older releases used metadata-v*.
_PNPM_METADATA_RULE = _rule(
    "pnpm-metadata-cache",
    "<vendor-metadata-cache>",
    MatchKind.EXACT,
    DecisionOwner.USER,
    RebuildCost.MEDIUM,
    "pnpm registry metadata mirror; may be required for offline resolution",
    root_kind="cache",
    requires_process_closed=True,
    user_age_buckets=(30, 90, 180),
)

_PNPM_CACHE_UNCLASSIFIED_RULE = _rule(
    "pnpm-cache-unclassified",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Unclassified pnpm cache-root state",
    root_kind="cache",
)

_PNPM_UPDATE_STATE_RULE = _rule(
    "pnpm-update-state",
    "pnpm-state.json",
    MatchKind.EXACT,
    DecisionOwner.TOOL,
    RebuildCost.NONE,
    "pnpm update-check throttle state",
    root_kind="state",
    idle_days=1,
    requires_process_closed=True,
    size_sensitive_idle=False,
)

_PNPM_STATE_UNCLASSIFIED_RULE = _rule(
    "pnpm-state-unclassified",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Unclassified pnpm state directory",
    root_kind="state",
)

_PNPM_STORE_RULE = _rule(
    "pnpm-store",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "pnpm store; maintain with source-bounded pnpm store prune",
    root_kind="store",
)

_PNPM_GLOBAL_RULE = _rule(
    "pnpm-global-install",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "pnpm globally installed packages",
    root_kind="global",
)

_PNPM_GLOBAL_BIN_RULE = _rule(
    "pnpm-global-bin",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "pnpm global executable shims",
    root_kind="global_bin",
)

_PNPM_HOME_RULE = _rule(
    "pnpm-home",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "PNPM_HOME executables, configuration and persistent package-manager data",
    root_kind="home",
)

PNPM_RULES: tuple[ApplicationCleanupRule, ...] = (
    _PNPM_DLX_RULE,
    _PNPM_METADATA_RULE,
    _PNPM_CACHE_UNCLASSIFIED_RULE,
    _PNPM_UPDATE_STATE_RULE,
    _PNPM_STATE_UNCLASSIFIED_RULE,
    _PNPM_STORE_RULE,
    _PNPM_GLOBAL_RULE,
    _PNPM_GLOBAL_BIN_RULE,
    _PNPM_HOME_RULE,
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
_CURRENT_METADATA_DIRS = frozenset(
    {"metadata", "metadata-full", "metadata-full-filtered"}
)


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
        stores.append(_store_config_root(PureWindowsPath(store_value)))

    if environment is None:
        active_store = _active_pnpm_store_path()
        if active_store:
            stores.append(_store_config_root(PureWindowsPath(active_store)))
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

    if any(
        _is_metadata_cache_path(normalized, cache)
        for cache in roots.cache_roots
    ):
        return _PNPM_METADATA_RULE

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
        if rule is _PNPM_METADATA_RULE:
            continue
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

    filename = PureWindowsPath(str(path)).name.casefold()
    if filename in _PNPM_METADATA_FILENAMES:
        return _PNPM_LOCK_RULE
    return None


def pnpm_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return raw whole-tree TOOL roots.

    Current pnpm has none. Store/DLX/metadata lifecycle stays vendor/user-owned,
    while the deterministic update-check object is one exact file, not a tree.
    """

    del environment
    return ()


def whole_tree_pnpm_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
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

    threshold = rule.idle_days
    score = _impl._benefit_score(logical_size, idle, threshold, rule.rebuild_cost)
    if rule.owner is DecisionOwner.USER:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.USER_DECISION,
            observed,
            idle,
            threshold,
            score,
        )

    running = process_running
    if running is None and rule.requires_process_closed:
        running = pnpm_process_running()
    action = (
        PolicyAction.TOOL_KEEP_IN_USE
        if rule.requires_process_closed and running
        else PolicyAction.TOOL_DELETE
    )
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


def _is_metadata_cache_path(
    normalized_path: str,
    cache_root: PureWindowsPath,
) -> bool:
    normalized_root = _impl._normalize(cache_root)
    prefix = normalized_root.rstrip("\\") + "\\"
    if not normalized_path.startswith(prefix):
        return False
    relative = normalized_path[len(prefix) :]
    parts = relative.split("\\")
    if not parts or not parts[0]:
        return False

    first = parts[0]
    if first.startswith("metadata-v") and len(first) > len("metadata-v"):
        return True
    if len(parts) < 2:
        return False
    version = first
    return (
        len(version) > 1
        and version.startswith("v")
        and version[1:].isdigit()
        and parts[1] in _CURRENT_METADATA_DIRS
    )


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


def _store_config_root(path: PureWindowsPath) -> PureWindowsPath:
    name = path.name.casefold()
    if len(name) > 1 and name.startswith("v") and name[1:].isdigit():
        return path.parent
    return path


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold().rstrip("\\/")
        if key and key not in seen:
            seen.add(key)
            found.append(path)
    return tuple(found)


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
