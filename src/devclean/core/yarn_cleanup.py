"""Audited Yarn Classic and modern Yarn storage semantics for Windows cleanup.

Yarn caches are source-identifiable, but current vendor cleanup surfaces are not a
safe substitute for DevClean's former raw whole-tree rules. Classic whole-cache
cleaning widens from ``cache dir`` to its parent cache root; modern cache cleaning
is project/configuration-sensitive and global mirror cleanup can trigger plugin
hooks. Machine caches therefore remain visible but protected from generic delete.
"""

from __future__ import annotations

import os
import re
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


@dataclass(frozen=True, slots=True)
class YarnRootSet:
    classic_cache_roots: tuple[PureWindowsPath, ...]
    global_folder_roots: tuple[PureWindowsPath, ...]
    global_cache_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_key: str,
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
    user_age_buckets: tuple[int, ...] = (),
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="yarn",
        root_key=root_key,
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=allow_whole_tree,
        user_age_buckets=user_age_buckets,
        label=label,
    )


_YARN_CLASSIC_CACHE_RULE = _rule(
    "yarn-classic-global-cache",
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Yarn Classic global package cache; generic raw deletion removed",
    root_key="YARN_CLASSIC_CACHE",
)
_YARN_GLOBAL_CACHE_RULE = _rule(
    "yarn-modern-global-cache",
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Yarn modern machine/global package cache; vendor lifecycle needs project-aware review",
    root_key="YARN_GLOBAL_CACHE",
)
_YARN_GLOBAL_FOLDER_RULE = _rule(
    "yarn-modern-global-state",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Yarn modern global state and content-addressable store",
    root_key="YARN_GLOBAL_FOLDER",
)
_YARN_LOCAL_CACHE_RULE = _rule(
    "yarn-project-offline-cache",
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Yarn project-local offline mirror / Zero-Installs cache",
    root_key="ANYWHERE",
    user_age_buckets=(30, 90, 180),
)
_YARN_PROJECT_STATE_RULE = _rule(
    "yarn-project-state",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Yarn project configuration, patches, releases, SDKs and install state",
    root_key="ANYWHERE",
)
_YARN_PROJECT_METADATA_RULE = _rule(
    "yarn-project-metadata",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Yarn lockfile, configuration and Plug'n'Play loader metadata",
    root_key="ANYWHERE",
)

YARN_RULES: tuple[ApplicationCleanupRule, ...] = (
    _YARN_CLASSIC_CACHE_RULE,
    _YARN_GLOBAL_CACHE_RULE,
    _YARN_GLOBAL_FOLDER_RULE,
    _YARN_LOCAL_CACHE_RULE,
    _YARN_PROJECT_STATE_RULE,
    _YARN_PROJECT_METADATA_RULE,
)

_PROJECT_METADATA_NAMES = frozenset(
    {
        "yarn.lock",
        ".yarnrc",
        ".yarnrc.yml",
        ".pnp.cjs",
        ".pnp.js",
        ".pnp.loader.mjs",
    }
)
_PROJECT_PROTECTED_CHILDREN = frozenset(
    {
        "patches",
        "plugins",
        "releases",
        "sdks",
        "versions",
        "unplugged",
        "install-state.gz",
        "build-state.yml",
    }
)


def yarn_roots(environment: Mapping[str, str] | None = None) -> YarnRootSet:
    env = _casefold_env(environment)
    classic: list[PureWindowsPath] = []
    global_folders: list[PureWindowsPath] = []
    global_caches: list[PureWindowsPath] = []

    explicit_classic = env.get("devclean_yarn_classic_cache_dir")
    explicit_global = env.get("devclean_yarn_global_folder")
    if explicit_classic:
        _append_absolute(classic, explicit_classic)
    if explicit_global:
        candidate = PureWindowsPath(explicit_global)
        if candidate.is_absolute():
            global_folders.append(candidate)
            global_caches.append(candidate / "cache")

    localappdata = env.get("localappdata")
    if localappdata:
        default_classic = PureWindowsPath(localappdata) / "Yarn" / "Cache"
        if _path_is_directory(default_classic):
            classic.append(default_classic)

    if environment is None:
        live = _live_yarn_paths()
        classic.extend(live.classic_cache_roots)
        global_folders.extend(live.global_folder_roots)
        global_caches.extend(live.global_cache_roots)

    return YarnRootSet(
        classic_cache_roots=_unique_paths(classic),
        global_folder_roots=_unique_paths(global_folders),
        global_cache_roots=_unique_paths(global_caches),
    )


def yarn_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = yarn_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.classic_cache_roots,
                *roots.global_folder_roots,
                *roots.global_cache_roots,
            )
        )
    )


def match_yarn_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    candidate = PureWindowsPath(str(path))
    project_rule = _project_rule(candidate)
    if project_rule is not None:
        return project_rule

    normalized = _impl._normalize(path)
    roots = yarn_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for root in roots.classic_cache_roots:
        _append_root_match(matches, normalized, root, _YARN_CLASSIC_CACHE_RULE, 0)
    for root in roots.global_cache_roots:
        _append_root_match(matches, normalized, root, _YARN_GLOBAL_CACHE_RULE, 0)
    for root in roots.global_folder_roots:
        _append_root_match(matches, normalized, root, _YARN_GLOBAL_FOLDER_RULE, 10_000)
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def yarn_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return no generic delete roots; Yarn cleanup needs a dedicated vendor lane."""

    del environment
    return ()


def whole_tree_yarn_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Raw whole-tree Yarn cache deletion is deliberately not authorized."""

    del path, environment
    return None


def evaluate_yarn_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_yarn_rule(path, environment)
    if rule is None:
        return None

    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0)
    if rule.owner is DecisionOwner.USER:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.USER_DECISION,
            observed,
            idle,
            None,
            _impl._benefit_score(logical_size, idle, None, rule.rebuild_cost),
            _impl._age_bucket(idle, rule.user_age_buckets),
        )

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = yarn_process_running()
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
def yarn_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'node.exe' -and $_.CommandLine -match "
        "'(?i)(?:[\\\\/]yarn(?:\\.js|\\.cjs)?(?:\\s|\")|@yarnpkg|corepack.+\\byarn\\b)' }; "
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
def _live_yarn_paths() -> YarnRootSet:
    version = _run_yarn(("--version",))
    if version is None:
        return YarnRootSet((), (), ())
    major = _major_version(version)
    if major == 1:
        cache = _run_yarn(("cache", "dir"))
        roots = () if cache is None else _absolute_tuple((cache,))
        return YarnRootSet(roots, (), ())

    global_folder = _run_yarn(("config", "get", "globalFolder"))
    folders = () if global_folder is None else _absolute_tuple((global_folder,))
    caches = tuple(folder / "cache" for folder in folders)
    return YarnRootSet((), folders, caches)


def clear_yarn_process_cache() -> None:
    yarn_process_running.cache_clear()
    _live_yarn_paths.cache_clear()


def _run_yarn(arguments: tuple[str, ...]) -> str | None:
    executable = "yarn.cmd" if os.name == "nt" else "yarn"
    try:
        result = subprocess.run(
            [executable, *arguments],
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
    value = lines[-1].strip().strip('"').strip("'")
    if not value or value.casefold() in {"undefined", "null"}:
        return None
    return value


def _major_version(value: str) -> int | None:
    match = re.match(r"\s*(\d+)", value)
    return int(match.group(1)) if match else None


def _project_rule(path: PureWindowsPath) -> ApplicationCleanupRule | None:
    name = path.name.casefold()
    if name in _PROJECT_METADATA_NAMES:
        return _YARN_PROJECT_METADATA_RULE
    parts = tuple(part.casefold() for part in path.parts)
    try:
        index = parts.index(".yarn")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return _YARN_PROJECT_STATE_RULE
    child = parts[index + 1]
    if child == "cache":
        return _YARN_LOCAL_CACHE_RULE
    if child in _PROJECT_PROTECTED_CHILDREN:
        return _YARN_PROJECT_STATE_RULE
    return _YARN_PROJECT_STATE_RULE


def _append_root_match(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
    index: int,
) -> None:
    normalized_root = _impl._normalize(root)
    if not _impl._matches(normalized_path, normalized_root, MatchKind.PREFIX):
        return
    owner_weight = 3 if rule.owner is DecisionOwner.KEEP else 1
    matches.append((len(normalized_root), owner_weight * 1000 - index, rule))


def _append_absolute(found: list[PureWindowsPath], value: str) -> None:
    candidate = PureWindowsPath(value)
    if candidate.is_absolute():
        found.append(candidate)


def _absolute_tuple(values: tuple[str, ...]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    for value in values:
        _append_absolute(found, value)
    return _unique_paths(found)


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


def _path_is_directory(path: PureWindowsPath) -> bool:
    try:
        return os.path.isdir(str(path))
    except OSError:
        return False


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "YARN_RULES",
    "YarnRootSet",
    "clear_yarn_process_cache",
    "evaluate_yarn_path",
    "match_yarn_rule",
    "whole_tree_yarn_rule",
    "yarn_audited_tool_roots",
    "yarn_process_running",
    "yarn_roots",
    "yarn_scan_roots",
]
