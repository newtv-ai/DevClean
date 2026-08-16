"""Audited npm storage semantics for Windows cleanup.

npm's configured cache, global installation prefix, log directory and config
files may all be redirected independently. The cache contains several npm-owned
subtrees, but the global prefix contains installed CLI packages and executable
shims. This profile grants deletion authority only to proven cache subtrees/log
files while protecting global installs and package metadata.
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
    effective_idle_days,
)

_MIB = 1024**2
_NPM_EXTERNAL_LOG_PATTERN = (
    "{????-??-??T??_??_??_???Z-debug-?.log,"
    "????-??-??T??_??_??_???Z-debug.log}"
)


@dataclass(frozen=True, slots=True)
class NpmRootSet:
    cache_roots: tuple[PureWindowsPath, ...]
    prefix_roots: tuple[PureWindowsPath, ...]
    external_logs_roots: tuple[PureWindowsPath, ...]
    user_config_files: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_kind: str = "cache",
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="npm",
        root_key=f"NPM_{root_kind.upper()}",
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


def _tool_cache_dir(
    rule_id: str,
    relative: str,
    label: str,
    *,
    idle_days: float,
    min_reclaim_bytes: int,
    rebuild_cost: RebuildCost,
) -> ApplicationCleanupRule:
    return _rule(
        rule_id,
        relative,
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        rebuild_cost,
        label,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=True,
        allow_whole_tree=True,
    )


NPM_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_cache_dir(
        "npm-content-cache",
        "_cacache",
        "npm content-addressable package cache",
        idle_days=30,
        min_reclaim_bytes=16 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_cache_dir(
        "npm-npx-cache",
        "_npx",
        "npm/npx temporary executable package installs",
        idle_days=30,
        min_reclaim_bytes=16 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_cache_dir(
        "npm-default-logs",
        "_logs",
        "npm diagnostic logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _rule(
        "npm-cache-unclassified",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified files at the npm cache root",
    ),
    _rule(
        "npm-global-prefix",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "npm global packages and executable shims",
        root_kind="prefix",
    ),
    _rule(
        "npm-external-debug-logs",
        _NPM_EXTERNAL_LOG_PATTERN,
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "npm diagnostic log in a configured logs-dir",
        root_kind="logs",
        idle_days=7,
        min_reclaim_bytes=256 * 1024,
        requires_process_closed=True,
        allow_whole_tree=False,
    ),
    _rule(
        "npm-external-logs-unclassified",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified files in a custom npm logs-dir",
        root_kind="logs",
    ),
)


_NPM_KEEP_FILENAMES = frozenset(
    {
        ".npmrc",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
    }
)

_NPM_METADATA_RULE = ApplicationCleanupRule(
    rule_id="npm-project-metadata",
    app_id="npm",
    root_key="ANYWHERE",
    relative_pattern="",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="npm project/config metadata",
)

_NPM_USERCONFIG_RULE = ApplicationCleanupRule(
    rule_id="npm-user-config",
    app_id="npm",
    root_key="NPM_USERCONFIG",
    relative_pattern="",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="configured npm user configuration",
)

_NPM_LEGACY_DEBUG_RULE = ApplicationCleanupRule(
    rule_id="npm-legacy-debug-log",
    app_id="npm",
    root_key="ANYWHERE",
    relative_pattern="npm-debug.log",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.NONE,
    idle_days=7,
    min_reclaim_bytes=256 * 1024,
    requires_process_closed=True,
    label="legacy npm debug log",
)


def npm_roots(environment: Mapping[str, str] | None = None) -> NpmRootSet:
    env = _casefold_env(environment)
    localappdata = env.get("localappdata")
    appdata = env.get("appdata")
    profile = env.get("userprofile")

    default_cache = (
        PureWindowsPath(localappdata) / "npm-cache" if localappdata else None
    )
    default_prefix = PureWindowsPath(appdata) / "npm" if appdata else None
    default_user_config = PureWindowsPath(profile) / ".npmrc" if profile else None

    effective = _effective_npm_config() if environment is None else {}
    cache_value = env.get("npm_config_cache") or effective.get("cache")
    prefix_value = env.get("npm_config_prefix") or effective.get("prefix")
    userconfig_value = env.get("npm_config_userconfig") or effective.get("userconfig")
    logs_value = env.get("npm_config_logs_dir") or effective.get("logs-dir")

    caches: list[PureWindowsPath] = []
    prefixes: list[PureWindowsPath] = []
    external_logs: list[PureWindowsPath] = []
    user_configs: list[PureWindowsPath] = []

    if cache_value:
        caches.append(PureWindowsPath(cache_value))
    if default_cache is not None:
        caches.append(default_cache)

    if prefix_value:
        prefixes.append(PureWindowsPath(prefix_value))
    if default_prefix is not None:
        prefixes.append(default_prefix)

    if userconfig_value:
        user_configs.append(PureWindowsPath(userconfig_value))
    if default_user_config is not None:
        user_configs.append(default_user_config)

    if logs_value and not _is_nullish_config(logs_value):
        log_root = PureWindowsPath(logs_value)
        default_log_roots = {str(cache / "_logs").casefold() for cache in caches}
        if str(log_root).casefold() not in default_log_roots:
            external_logs.append(log_root)

    return NpmRootSet(
        cache_roots=_unique_paths(caches),
        prefix_roots=_unique_paths(prefixes),
        external_logs_roots=_unique_paths(external_logs),
        user_config_files=_unique_paths(user_configs),
    )


def npm_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = npm_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.cache_roots,
                *roots.prefix_roots,
                *roots.external_logs_roots,
            )
        )
    )


def match_npm_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = npm_roots(environment)

    if any(normalized == _impl._normalize(config) for config in roots.user_config_files):
        return _NPM_USERCONFIG_RULE

    groups = {
        "NPM_CACHE": roots.cache_roots,
        "NPM_PREFIX": roots.prefix_roots,
        "NPM_LOGS": roots.external_logs_roots,
    }
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(NPM_RULES):
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
    if filename in _NPM_KEEP_FILENAMES:
        return _NPM_METADATA_RULE
    if filename == "npm-debug.log":
        return _NPM_LEGACY_DEBUG_RULE
    return None


def npm_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return only exact npm-owned cache subtrees, never the configured root."""

    roots = npm_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for rule in NPM_RULES:
        if (
            rule.owner is not DecisionOwner.TOOL
            or not rule.allow_whole_tree
            or rule.root_key != "NPM_CACHE"
        ):
            continue
        if any(token in rule.relative_pattern for token in ("*", "?", "[", "{")):
            continue
        for root in roots.cache_roots:
            path = root / rule.relative_pattern
            key = _impl._normalize(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((path, rule))
    return tuple(found)


def whole_tree_npm_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in npm_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_npm_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_npm_rule(path, environment)
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
        running = npm_process_running()
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
def npm_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'node.exe' -and ($_.CommandLine -match "
        "'(?i)(npm-cli\\.js|npx-cli\\.js|npm\\s+exec)') }; "
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
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


@lru_cache(maxsize=1)
def _effective_npm_config() -> dict[str, str]:
    """Read non-secret effective paths from npm once when the CLI is available."""

    executable = "npm.cmd" if os.name == "nt" else "npm"
    try:
        result = subprocess.run(
            [
                executable,
                "config",
                "get",
                "cache",
                "prefix",
                "userconfig",
                "logs-dir",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip().casefold()
        value = value.strip()
        if key in {"cache", "prefix", "userconfig", "logs-dir"} and value:
            values[key] = value
    return values


def clear_npm_process_cache() -> None:
    npm_process_running.cache_clear()
    _effective_npm_config.cache_clear()


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold().rstrip("\\/")
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(path)
    return tuple(found)


def _is_nullish_config(value: str) -> bool:
    return value.strip().casefold() in {"null", "undefined", "none", ""}


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "NPM_RULES",
    "NpmRootSet",
    "clear_npm_process_cache",
    "evaluate_npm_path",
    "match_npm_rule",
    "npm_audited_tool_roots",
    "npm_process_running",
    "npm_roots",
    "npm_scan_roots",
    "whole_tree_npm_rule",
]
