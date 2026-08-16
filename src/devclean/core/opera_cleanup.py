"""Audited Opera / Opera GX storage semantics for Windows cleanup.

Opera is Chromium-based but deliberately splits authoritative roaming profile
state from local cache storage on normal Windows installations. Newer releases
also use a Chromium-style ``Default`` profile subdirectory while older layouts
stored profile/cache children directly below the edition root. This module
handles both layouts without ever granting whole-tree authority to the edition
roots themselves.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from devclean.core.chrome_cleanup import CHROME_RULES

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class OperaRootSet:
    roaming_roots: tuple[PureWindowsPath, ...]
    local_roots: tuple[PureWindowsPath, ...]
    disk_cache_roots: tuple[PureWindowsPath, ...]


def _clone_profile_rule(rule: ApplicationCleanupRule) -> ApplicationCleanupRule:
    return replace(
        rule,
        rule_id=rule.rule_id.replace("chrome-", "opera-", 1),
        app_id="opera",
        root_key="OPERA_PROFILE",
        label=rule.label.replace("Chrome", "Opera"),
    )


def _clone_data_rule(rule: ApplicationCleanupRule) -> ApplicationCleanupRule:
    return replace(
        rule,
        rule_id=rule.rule_id.replace("chrome-", "opera-", 1),
        app_id="opera",
        root_key="OPERA_DATA",
        label=rule.label.replace("Chrome", "Opera"),
    )


_OPERA_PROFILE_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _clone_profile_rule(rule)
    for rule in CHROME_RULES
    if rule.root_key == "CHROME_PROFILE"
)
_OPERA_DATA_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _clone_data_rule(rule)
    for rule in CHROME_RULES
    if rule.root_key == "CHROME_DATA" and rule.owner is DecisionOwner.TOOL
)
_OPERA_DISK_CACHE_RULE = replace(
    next(rule for rule in CHROME_RULES if rule.root_key == "CHROME_DISK_CACHE"),
    rule_id="opera-explicit-disk-cache",
    app_id="opera",
    root_key="OPERA_DISK_CACHE",
    label="Opera explicitly dedicated disk cache",
)

# Opera-specific cache directory exposed in the vendor support community and in
# current Opera GX cache layouts. It is cache-only and can be regenerated.
_OPERA_SYSTEM_CACHE_RULE = ApplicationCleanupRule(
    rule_id="opera-system-cache",
    app_id="opera",
    root_key="OPERA_PROFILE",
    relative_pattern="System Cache",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.LOW,
    idle_days=14,
    min_reclaim_bytes=8 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
    label="Opera Chromium system cache",
)

# Profiles manually renamed during Opera update/recovery incidents can be the
# only remaining copy of tabs/settings. Surface them to the user, never purge.
_OPERA_PROFILE_BACKUP_RULE = ApplicationCleanupRule(
    rule_id="opera-profile-recovery-copy",
    app_id="opera",
    root_key="OPERA_EDITION",
    relative_pattern="Default.old",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.USER,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    user_age_buckets=(30, 90, 180),
    label="Opera old/recovery Default profile copy",
)

_OPERA_EDITION_STATE_RULE = ApplicationCleanupRule(
    rule_id="opera-edition-state",
    app_id="opera",
    root_key="OPERA_EDITION",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Opera profile/cache edition root with mixed persistent state",
)

OPERA_RULES: tuple[ApplicationCleanupRule, ...] = (
    *_OPERA_PROFILE_RULES,
    *_OPERA_DATA_RULES,
    _OPERA_SYSTEM_CACHE_RULE,
    _OPERA_PROFILE_BACKUP_RULE,
    _OPERA_EDITION_STATE_RULE,
    _OPERA_DISK_CACHE_RULE,
)

_EDITION_NAMES = (
    "Opera Stable",
    "Opera Next",
    "Opera Developer",
    "Opera GX Stable",
)


def opera_roots(environment: Mapping[str, str] | None = None) -> OperaRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")

    roaming: list[PureWindowsPath] = []
    local: list[PureWindowsPath] = []
    disk_cache: list[PureWindowsPath] = []

    if appdata:
        base = PureWindowsPath(appdata) / "Opera Software"
        roaming.extend(base / name for name in _EDITION_NAMES)
    if localappdata:
        base = PureWindowsPath(localappdata) / "Opera Software"
        local.extend(base / name for name in _EDITION_NAMES)

    explicit_profile = env.get("devclean_opera_profile_dir")
    explicit_cache = env.get("devclean_opera_cache_dir")
    explicit_disk_cache = env.get("devclean_opera_disk_cache_dir")
    if explicit_profile:
        roaming.insert(0, PureWindowsPath(explicit_profile))
    if explicit_cache:
        local.insert(0, PureWindowsPath(explicit_cache))
    if explicit_disk_cache:
        disk_cache.insert(0, PureWindowsPath(explicit_disk_cache))

    if environment is None:
        running_profiles, running_caches, portable = _running_override_roots()
        roaming[0:0] = running_profiles
        local[0:0] = running_caches
        # Standalone/USB installs keep profile and cache in one data tree. Add
        # it to both semantic root sets so exact cache children can be reclaimed
        # while every unknown child remains protected by the edition fallback.
        roaming[0:0] = portable
        local[0:0] = portable

    return OperaRootSet(
        roaming_roots=_unique_paths(roaming),
        local_roots=_unique_paths(local),
        disk_cache_roots=_unique_paths(disk_cache),
    )


def opera_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = opera_roots(environment)
    return tuple(
        dict.fromkeys(
            (*roots.roaming_roots, *roots.local_roots, *roots.disk_cache_roots)
        )
    )


def match_opera_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = opera_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.disk_cache_roots:
        _append_match(matches, normalized, root, _OPERA_DISK_CACHE_RULE, 0)

    # Roaming is authoritative profile state, but exact Chromium-generated cache
    # subtrees are still safe. Support both legacy direct children and current
    # Default/Profile-N layouts.
    for root in roots.roaming_roots:
        _append_edition_matches(matches, normalized, path, root, include_http_cache=False)

    # Local roots contain Opera's disk cache. Unknown local children are still
    # protected instead of assuming the entire root is disposable.
    for root in roots.local_roots:
        _append_edition_matches(matches, normalized, path, root, include_http_cache=True)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def opera_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = opera_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()

    # Roaming: only generated code/GPU/shader/script caches, not HTTP Cache or
    # System Cache. This avoids accidentally delegating legacy mixed data.
    for root in roots.roaming_roots:
        for profile_root in _candidate_profile_roots(root):
            for rule in _OPERA_PROFILE_RULES:
                if rule.owner is not DecisionOwner.TOOL:
                    continue
                if rule.rule_id in {"opera-http-cache", "opera-media-cache"}:
                    continue
                _append_tool_root(found, seen, profile_root, rule)
        for rule in _OPERA_DATA_RULES:
            _append_tool_root(found, seen, root, rule)

    # Local cache roots: exact Chromium cache children and Opera System Cache.
    for root in roots.local_roots:
        for profile_root in _candidate_profile_roots(root):
            for rule in _OPERA_PROFILE_RULES:
                if rule.owner is DecisionOwner.TOOL:
                    _append_tool_root(found, seen, profile_root, rule)
            _append_tool_root(found, seen, profile_root, _OPERA_SYSTEM_CACHE_RULE)
        for rule in _OPERA_DATA_RULES:
            _append_tool_root(found, seen, root, rule)

    for root in roots.disk_cache_roots:
        _append_tool_root(found, seen, root, _OPERA_DISK_CACHE_RULE)
    return tuple(found)


def whole_tree_opera_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in opera_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_opera_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_opera_rule(path, environment)
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
            rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0
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
        running = opera_process_running()
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
def opera_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'opera.exe' -or $_.Name -ieq 'opera_autoupdate.exe' }; "
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
def _running_override_roots() -> tuple[
    tuple[PureWindowsPath, ...],
    tuple[PureWindowsPath, ...],
    tuple[PureWindowsPath, ...],
]:
    if os.name != "nt":
        return (), (), ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'opera.exe' }; "
        "$p | ForEach-Object { \"{0}`t{1}\" -f $_.ExecutablePath,$_.CommandLine }"
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
        return (), (), ()
    if result.returncode != 0:
        return (), (), ()

    profiles: list[PureWindowsPath] = []
    caches: list[PureWindowsPath] = []
    portable: list[PureWindowsPath] = []
    for line in result.stdout.splitlines():
        executable, _, command_line = line.partition("\t")
        profile = _switch_path(command_line, "user-data-dir")
        if profile:
            profiles.append(PureWindowsPath(profile))
        cache = _switch_path(command_line, "disk-cache-dir")
        if cache:
            caches.append(PureWindowsPath(cache))
        if executable:
            portable_root = _portable_data_root(executable)
            if portable_root is not None:
                portable.append(portable_root)
    return _unique_paths(profiles), _unique_paths(caches), _unique_paths(portable)


def _portable_data_root(executable: str) -> PureWindowsPath | None:
    """Return `<install>/profile/data` only when it actually exists."""

    exe = PureWindowsPath(executable)
    candidates = (exe.parent / "profile" / "data", exe.parent.parent / "profile" / "data")
    for candidate in candidates:
        try:
            if Path(str(candidate)).is_dir():
                return candidate
        except OSError:
            continue
    return None


def clear_opera_process_cache() -> None:
    opera_process_running.cache_clear()
    _running_override_roots.cache_clear()


def _append_edition_matches(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    raw_path: str | os.PathLike[str],
    root: PureWindowsPath,
    *,
    include_http_cache: bool,
) -> None:
    # User-created/recovery `Default.old*` copies outrank any nested cache name.
    _append_match(matches, normalized_path, root, _OPERA_PROFILE_BACKUP_RULE, 0)

    profile_root = _profile_root_for_path(raw_path, root)
    if profile_root is not None:
        _append_profile_rules(
            matches,
            normalized_path,
            profile_root,
            include_http_cache=include_http_cache,
        )

    # Legacy Opera layouts used the edition root itself as the profile/cache
    # root. Current layouts can still leave direct cache children after upgrades.
    _append_profile_rules(
        matches,
        normalized_path,
        root,
        include_http_cache=include_http_cache,
    )
    for index, rule in enumerate(_OPERA_DATA_RULES):
        _append_match(matches, normalized_path, root, rule, index)
    _append_match(matches, normalized_path, root, _OPERA_EDITION_STATE_RULE, 10_000)


def _append_profile_rules(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    profile_root: PureWindowsPath,
    *,
    include_http_cache: bool,
) -> None:
    for index, rule in enumerate(_OPERA_PROFILE_RULES):
        if (
            not include_http_cache
            and rule.owner is DecisionOwner.TOOL
            and rule.rule_id in {"opera-http-cache", "opera-media-cache"}
        ):
            continue
        _append_match(matches, normalized_path, profile_root, rule, index)
    if include_http_cache:
        _append_match(
            matches,
            normalized_path,
            profile_root,
            _OPERA_SYSTEM_CACHE_RULE,
            len(_OPERA_PROFILE_RULES),
        )


def _profile_root_for_path(
    path: str | os.PathLike[str], edition_root: PureWindowsPath
) -> PureWindowsPath | None:
    target = PureWindowsPath(os.fspath(path))
    try:
        relative = target.relative_to(edition_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if not _is_profile_dir_name(name):
        return None
    return edition_root / name


def _candidate_profile_roots(root: PureWindowsPath) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = [root]
    path = Path(str(root))
    try:
        children = tuple(path.iterdir())
    except OSError:
        return tuple(found)
    for child in children:
        if child.is_dir() and _is_profile_dir_name(child.name):
            found.append(PureWindowsPath(str(child)))
    return tuple(found)


def _is_profile_dir_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in {"default", "guest profile", "system profile"}
        or lowered.startswith("profile ")
    )


def _switch_path(command_line: str, switch: str) -> str | None:
    pattern = re.compile(
        rf"--{re.escape(switch)}(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


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
    "OPERA_RULES",
    "OperaRootSet",
    "clear_opera_process_cache",
    "evaluate_opera_path",
    "match_opera_rule",
    "opera_audited_tool_roots",
    "opera_process_running",
    "opera_roots",
    "opera_scan_roots",
    "whole_tree_opera_rule",
]
