"""Audited Google Chrome / Chromium storage semantics for Windows cleanup.

Chromium keeps browser profile state and multiple regenerable caches under the
same user-data tree. This profile therefore grants whole-tree authority only to
cache directories proven by Chromium source, while the user-data root and all
unclassified profile state remain protected.

The Chromium Updater is handled with the same boundary: its downloaded CRX
payload cache is regenerable, but updater binaries/preferences and legacy
updater state are not treated as generic garbage.
"""

from __future__ import annotations

import os
import re
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

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class ChromeRootSet:
    data_roots: tuple[PureWindowsPath, ...]
    disk_cache_roots: tuple[PureWindowsPath, ...]
    updater_roots: tuple[PureWindowsPath, ...]
    legacy_updater_roots: tuple[PureWindowsPath, ...]


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
        app_id="chrome",
        root_key=f"CHROME_{root_kind.upper()}",
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


def _tool_dir(
    rule_id: str,
    relative: str,
    label: str,
    *,
    root_kind: str,
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
        root_kind=root_kind,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=True,
        allow_whole_tree=True,
    )


# Root-level caches created directly below Chromium's user-data directory.
_CHROME_DATA_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_dir(
        "chrome-component-crx-cache",
        "component_crx_cache",
        "Chrome component-updater CRX cache",
        root_kind="data",
        idle_days=14,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_dir(
        "chrome-shader-cache",
        "ShaderCache",
        "Chrome GPU shader cache",
        root_kind="data",
        idle_days=7,
        min_reclaim_bytes=4 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _tool_dir(
        "chrome-grshader-cache",
        "GrShaderCache",
        "Chrome graphics shader cache",
        root_kind="data",
        idle_days=7,
        min_reclaim_bytes=4 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _tool_dir(
        "chrome-graphite-dawn-cache",
        "GraphiteDawnCache",
        "Chrome Graphite/Dawn graphics cache",
        root_kind="data",
        idle_days=7,
        min_reclaim_bytes=4 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _tool_dir(
        "chrome-gpu-persistent-cache",
        "GPUPersistentCache",
        "Chrome persistent GPU cache",
        root_kind="data",
        idle_days=7,
        min_reclaim_bytes=4 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _tool_dir(
        "chrome-font-lookup-cache",
        "FontLookupTableCache",
        "Chrome font lookup table cache",
        root_kind="data",
        idle_days=14,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _rule(
        "chrome-user-data-state",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chrome user-data state, profiles and browser configuration",
        root_kind="data",
    ),
)


# Per-profile caches are matched against an exact discovered profile root
# (Default / Profile N / Guest Profile / System Profile), not with a broad
# recursive glob, so nested folders merely named "Cache" cannot inherit browser
# deletion authority.
_CHROME_PROFILE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_dir(
        "chrome-http-cache",
        "Cache",
        "Chrome HTTP resource cache",
        root_kind="profile",
        idle_days=14,
        min_reclaim_bytes=16 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_dir(
        "chrome-media-cache",
        "Media Cache",
        "Chrome media resource cache",
        root_kind="profile",
        idle_days=14,
        min_reclaim_bytes=16 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_dir(
        "chrome-code-cache",
        "Code Cache",
        "Chrome generated JavaScript/WebAssembly code cache",
        root_kind="profile",
        idle_days=14,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _tool_dir(
        "chrome-profile-gpu-cache",
        "GPUCache",
        "Chrome per-profile GPU cache",
        root_kind="profile",
        idle_days=7,
        min_reclaim_bytes=4 * _MIB,
        rebuild_cost=RebuildCost.LOW,
    ),
    _rule(
        "chrome-site-cache-storage",
        r"Service Worker\CacheStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Chrome persistent per-site Cache Storage / offline web data",
        root_kind="profile",
        user_age_buckets=(30, 90, 180),
    ),
    _tool_dir(
        "chrome-service-worker-script-cache",
        r"Service Worker\ScriptCache",
        "Chrome service-worker script cache",
        root_kind="profile",
        idle_days=30,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _rule(
        "chrome-service-worker-state",
        "Service Worker",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chrome service-worker registrations and unclassified persistent state",
        root_kind="profile",
    ),
    _rule(
        "chrome-profile-state",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chrome history, cookies, logins, extensions, site data and preferences",
        root_kind="profile",
    ),
)


_CHROME_DISK_CACHE_RULE = _rule(
    "chrome-explicit-disk-cache",
    "",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.MEDIUM,
    "Chrome explicitly configured disk-cache directory",
    root_kind="disk_cache",
    idle_days=14,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)

_CHROME_UPDATER_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_dir(
        "chrome-updater-crx-cache",
        "crx_cache",
        "Google/Chromium Updater downloaded application payload cache",
        root_kind="updater",
        idle_days=7,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _rule(
        "chrome-updater-log",
        "updater.log",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chromium Updater current diagnostic log (vendor-rotated)",
        root_kind="updater",
    ),
    _rule(
        "chrome-updater-old-log",
        "updater.log.old",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chromium Updater rotated diagnostic log (vendor-managed)",
        root_kind="updater",
    ),
    _rule(
        "chrome-updater-state",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chromium Updater binaries, prefs and active version state",
        root_kind="updater",
    ),
)

_CHROME_LEGACY_UPDATER_RULE = _rule(
    "chrome-legacy-updater-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Legacy Google Update/Omaha state pending a separate audited cleanup path",
    root_kind="legacy_updater",
)

CHROME_RULES: tuple[ApplicationCleanupRule, ...] = (
    *_CHROME_DATA_RULES,
    *_CHROME_PROFILE_RULES,
    _CHROME_DISK_CACHE_RULE,
    *_CHROME_UPDATER_RULES,
    _CHROME_LEGACY_UPDATER_RULE,
)


def chrome_roots(environment: Mapping[str, str] | None = None) -> ChromeRootSet:
    env = _casefold_env(environment)
    localappdata = env.get("localappdata")
    programfiles_x86 = env.get("programfiles(x86)") or env.get("programfilesx86")

    data: list[PureWindowsPath] = []
    disk_cache: list[PureWindowsPath] = []
    updater: list[PureWindowsPath] = []
    legacy_updater: list[PureWindowsPath] = []

    if localappdata:
        base = PureWindowsPath(localappdata)
        data.extend(
            (
                base / "Google" / "Chrome" / "User Data",
                base / "Google" / "Chrome Beta" / "User Data",
                base / "Google" / "Chrome Dev" / "User Data",
                base / "Google" / "Chrome SxS" / "User Data",
                base / "Google" / "Chrome for Testing" / "User Data",
                base / "Chromium" / "User Data",
            )
        )
        updater.append(base / "Google" / "GoogleUpdater")
        legacy_updater.append(base / "Google" / "Update")

    if programfiles_x86:
        base = PureWindowsPath(programfiles_x86)
        updater.append(base / "Google" / "GoogleUpdater")
        legacy_updater.append(base / "Google" / "Update")

    # These are DevClean-only test/explicit override hooks. They are deliberately
    # not named like browser environment variables because Windows Chrome uses
    # command-line/policy configuration rather than CHROME_* environment paths.
    explicit_data = env.get("devclean_chrome_user_data_dir")
    explicit_cache = env.get("devclean_chrome_disk_cache_dir")
    if explicit_data:
        data.insert(0, PureWindowsPath(explicit_data))
    if explicit_cache:
        disk_cache.insert(0, PureWindowsPath(explicit_cache))

    if environment is None:
        running_data, running_cache = _running_override_roots()
        data[0:0] = running_data
        disk_cache[0:0] = running_cache

    return ChromeRootSet(
        data_roots=_unique_paths(data),
        disk_cache_roots=_unique_paths(disk_cache),
        updater_roots=_unique_paths(updater),
        legacy_updater_roots=_unique_paths(legacy_updater),
    )


def chrome_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = chrome_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.data_roots,
                *roots.disk_cache_roots,
                *roots.updater_roots,
                *roots.legacy_updater_roots,
            )
        )
    )


def match_chrome_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = chrome_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.disk_cache_roots:
        _append_match(matches, normalized, root, _CHROME_DISK_CACHE_RULE, 0)

    for root in roots.updater_roots:
        for index, rule in enumerate(_CHROME_UPDATER_RULES):
            _append_match(matches, normalized, root, rule, index)

    for root in roots.legacy_updater_roots:
        _append_match(matches, normalized, root, _CHROME_LEGACY_UPDATER_RULE, 0)

    for root in roots.data_roots:
        profile_root = _profile_root_for_path(path, root)
        if profile_root is not None:
            for index, rule in enumerate(_CHROME_PROFILE_RULES):
                _append_match(matches, normalized, profile_root, rule, index)
        for index, rule in enumerate(_CHROME_DATA_RULES):
            _append_match(matches, normalized, root, rule, index)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def chrome_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = chrome_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()

    for root in roots.data_roots:
        for rule in _CHROME_DATA_RULES:
            _append_tool_root(found, seen, root, rule)
        for profile_root in _existing_profile_roots(root):
            for rule in _CHROME_PROFILE_RULES:
                _append_tool_root(found, seen, profile_root, rule)

    for root in roots.disk_cache_roots:
        _append_tool_root(found, seen, root, _CHROME_DISK_CACHE_RULE)

    for root in roots.updater_roots:
        for rule in _CHROME_UPDATER_RULES:
            _append_tool_root(found, seen, root, rule)

    return tuple(found)


def whole_tree_chrome_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in chrome_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_chrome_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_chrome_rule(path, environment)
    if rule is None:
        return None

    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)

    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.KEEP_PROTECTED,
            observed,
            idle,
            None,
            0,
        )
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
        running = chrome_process_running()
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
def chrome_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'chrome.exe' -or $_.Name -ieq 'chromium.exe' -or "
        "$_.Name -ieq 'GoogleUpdate.exe' -or "
        "($_.Name -ieq 'updater.exe' -and $_.CommandLine -match "
        "'(?i)\\\\Google\\\\GoogleUpdater\\\\') }; "
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
def _running_override_roots() -> tuple[tuple[PureWindowsPath, ...], tuple[PureWindowsPath, ...]]:
    if os.name != "nt":
        return (), ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'chrome.exe' -or $_.Name -ieq 'chromium.exe' }; "
        "$p | ForEach-Object { $_.CommandLine }"
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
        return (), ()
    if result.returncode != 0:
        return (), ()

    data: list[PureWindowsPath] = []
    cache: list[PureWindowsPath] = []
    for line in result.stdout.splitlines():
        value = _switch_path(line, "user-data-dir")
        if value:
            data.append(PureWindowsPath(value))
        value = _switch_path(line, "disk-cache-dir")
        if value:
            cache.append(PureWindowsPath(value))
    return _unique_paths(data), _unique_paths(cache)


def clear_chrome_process_cache() -> None:
    chrome_process_running.cache_clear()
    _running_override_roots.cache_clear()


def _switch_path(command_line: str, switch: str) -> str | None:
    pattern = re.compile(
        rf"--{re.escape(switch)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


def _profile_root_for_path(
    path: str | os.PathLike[str], data_root: PureWindowsPath
) -> PureWindowsPath | None:
    target = PureWindowsPath(os.fspath(path))
    try:
        relative = target.relative_to(data_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not _is_profile_dir_name(name):
        return None
    return data_root / name


def _existing_profile_roots(data_root: PureWindowsPath) -> tuple[PureWindowsPath, ...]:
    path = Path(str(data_root))
    try:
        children = tuple(path.iterdir())
    except OSError:
        return ()
    found: list[PureWindowsPath] = []
    for child in children:
        if child.is_dir() and _is_profile_dir_name(child.name):
            found.append(PureWindowsPath(str(child)))
    return tuple(found)


def _is_profile_dir_name(name: str) -> bool:
    lowered = name.casefold()
    return lowered in {"default", "guest profile", "system profile"} or lowered.startswith(
        "profile "
    )


def _append_match(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
    index: int,
) -> None:
    normalized_root = _impl._normalize(root)
    for expanded in _impl._expand_braces(rule.relative_pattern):
        candidate = normalized_root + ("\\" + expanded if expanded else "")
        if not _impl._matches(normalized_path, candidate, rule.match_kind):
            continue
        if rule.owner is DecisionOwner.KEEP:
            owner_weight = 3
        elif rule.owner is DecisionOwner.USER:
            owner_weight = 2
        else:
            owner_weight = 1
        matches.append((len(candidate), owner_weight * 1000 - index, rule))


def _append_tool_root(
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]],
    seen: set[str],
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
) -> None:
    if rule.owner is not DecisionOwner.TOOL or not rule.allow_whole_tree:
        return
    if any(token in rule.relative_pattern for token in ("*", "?", "[", "{")):
        return
    path = root / rule.relative_pattern if rule.relative_pattern else root
    key = _impl._normalize(path)
    if key in seen:
        return
    seen.add(key)
    found.append((path, rule))


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
    "CHROME_RULES",
    "ChromeRootSet",
    "chrome_audited_tool_roots",
    "chrome_process_running",
    "chrome_roots",
    "chrome_scan_roots",
    "clear_chrome_process_cache",
    "evaluate_chrome_path",
    "match_chrome_rule",
    "whole_tree_chrome_rule",
]
