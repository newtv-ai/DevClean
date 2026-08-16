"""Audited Mozilla Firefox storage semantics for Windows cleanup.

Firefox explicitly distinguishes the persistent profile root (ProfD) from the
profile-local directory (ProfLD). In the default Windows layout ProfD lives in
Roaming AppData while ProfLD lives in Local AppData, and Mozilla documents the
latter as caches that can safely be deleted. Custom profiles may co-locate the
two, so only exact cache subtrees are delegated there. Firefox update roots are
stateful and therefore remain protected; only diagnostic logs are generic TOOL
data until update.mar state is handled by a dedicated maintenance action.
"""

from __future__ import annotations

import configparser
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

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class FirefoxRootSet:
    persistent_parents: tuple[PureWindowsPath, ...]
    local_parents: tuple[PureWindowsPath, ...]
    custom_profiles: tuple[PureWindowsPath, ...]
    crash_roots: tuple[PureWindowsPath, ...]
    update_parents: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative: str,
    kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_key: str,
    idle_days: int | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
    user_age_buckets: tuple[int, ...] = (),
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="firefox",
        root_key=root_key,
        relative_pattern=relative,
        match_kind=kind,
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


_FIREFOX_LOCAL_PROFILE_RULE = _rule(
    "firefox-local-profile-cache-root",
    "",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.MEDIUM,
    "Firefox profile-local cache directory (ProfLD)",
    root_key="FIREFOX_LOCAL_PROFILE",
    idle_days=30,
    min_reclaim_bytes=32 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)

_FIREFOX_CACHE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _rule(
        "firefox-cache2",
        "cache2",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        RebuildCost.MEDIUM,
        "Firefox HTTP disk cache (cache2)",
        root_key="FIREFOX_PROFILE",
        idle_days=14,
        min_reclaim_bytes=16 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "firefox-startup-cache",
        "startupCache",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        RebuildCost.LOW,
        "Firefox generated startup and WebExtension startup cache",
        root_key="FIREFOX_PROFILE",
        idle_days=14,
        min_reclaim_bytes=4 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "firefox-jumplist-cache",
        "jumpListCache",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        RebuildCost.LOW,
        "Firefox Windows Jump List favicon cache",
        root_key="FIREFOX_PROFILE",
        idle_days=30,
        min_reclaim_bytes=4 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
)

_FIREFOX_PERSISTENT_PROFILE_RULE = _rule(
    "firefox-persistent-profile-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Firefox persistent profile: bookmarks, logins, history, sessions, extensions and site data",
    root_key="FIREFOX_PROFILE",
)

_FIREFOX_CRASH_PENDING_RULE = _rule(
    "firefox-pending-crash-reports",
    "pending",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.NONE,
    "Firefox unsubmitted crash dumps and diagnostic metadata",
    root_key="FIREFOX_CRASH",
    idle_days=7,
    min_reclaim_bytes=_MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
)

_FIREFOX_CRASH_STATE_RULE = _rule(
    "firefox-crash-report-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.LOW,
    "Firefox crash-report index, submitted report references and configuration",
    root_key="FIREFOX_CRASH",
)

_FIREFOX_UPDATE_LOG_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _rule(
        f"firefox-update-log-{index}",
        pattern,
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "Firefox updater diagnostic log",
        root_key="FIREFOX_UPDATE_PARENT",
        idle_days=14,
        min_reclaim_bytes=256 * 1024,
        requires_process_closed=True,
        size_sensitive_idle=False,
    )
    for index, pattern in enumerate(
        (
            r"*\updates\0\update.log",
            r"*\updates\0\update-elevated.log",
            r"*\backup-update.log",
            r"*\backup-update-elevated.log",
            r"*\last-update.log",
            r"*\last-update-elevated.log",
        ),
        start=1,
    )
)

_FIREFOX_UPDATE_STATE_RULE = _rule(
    "firefox-update-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Firefox installation-specific update state and update.mar payloads",
    root_key="FIREFOX_UPDATE_PARENT",
)

FIREFOX_RULES: tuple[ApplicationCleanupRule, ...] = (
    _FIREFOX_LOCAL_PROFILE_RULE,
    *_FIREFOX_CACHE_RULES,
    _FIREFOX_PERSISTENT_PROFILE_RULE,
    _FIREFOX_CRASH_PENDING_RULE,
    _FIREFOX_CRASH_STATE_RULE,
    *_FIREFOX_UPDATE_LOG_RULES,
    _FIREFOX_UPDATE_STATE_RULE,
)


def firefox_roots(environment: Mapping[str, str] | None = None) -> FirefoxRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    programdata = env.get("programdata") or env.get("allusersprofile")

    persistent_parents: list[PureWindowsPath] = []
    local_parents: list[PureWindowsPath] = []
    custom_profiles: list[PureWindowsPath] = []
    crash_roots: list[PureWindowsPath] = []
    update_parents: list[PureWindowsPath] = []

    firefox_base: PureWindowsPath | None = None
    if appdata:
        firefox_base = PureWindowsPath(appdata) / "Mozilla" / "Firefox"
        persistent_parents.append(firefox_base / "Profiles")
        crash_roots.append(firefox_base / "Crash Reports")
        custom_profiles.extend(_profiles_ini_paths(firefox_base, localappdata))
    if localappdata:
        local_base = PureWindowsPath(localappdata)
        local_parents.append(local_base / "Mozilla" / "Firefox" / "Profiles")
        _append_msix_roots(
            local_base,
            persistent_parents,
            local_parents,
            crash_roots,
        )
    if programdata:
        update_parents.append(PureWindowsPath(programdata) / "Mozilla" / "updates")

    explicit_profile = env.get("devclean_firefox_profile_dir")
    explicit_local = env.get("devclean_firefox_local_profile_dir")
    explicit_update = env.get("devclean_firefox_update_parent")
    if explicit_profile:
        custom_profiles.insert(0, PureWindowsPath(explicit_profile))
    if explicit_local:
        local_parents.insert(0, PureWindowsPath(explicit_local).parent)
    if explicit_update:
        update_parents.insert(0, PureWindowsPath(explicit_update))

    return FirefoxRootSet(
        persistent_parents=_unique_paths(persistent_parents),
        local_parents=_unique_paths(local_parents),
        custom_profiles=_unique_paths(custom_profiles),
        crash_roots=_unique_paths(crash_roots),
        update_parents=_unique_paths(update_parents),
    )


def firefox_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = firefox_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.persistent_parents,
                *roots.local_parents,
                *roots.custom_profiles,
                *roots.crash_roots,
                *roots.update_parents,
            )
        )
    )


def match_firefox_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = PureWindowsPath(os.fspath(path))
    normalized = _impl._normalize(target)
    roots = firefox_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    # Default/MSIX local profile parents are Firefox cache-only ProfLD roots.
    for parent in roots.local_parents:
        profile = _child_root_for_path(target, parent)
        if profile is not None:
            _append_match(matches, normalized, profile, _FIREFOX_LOCAL_PROFILE_RULE, 100)
            for index, rule in enumerate(_FIREFOX_CACHE_RULES):
                _append_match(matches, normalized, profile, rule, index)

    # Persistent profile parents and custom profiles remain authoritative. Exact
    # cache children may be delegated because custom profiles can co-locate
    # ProfLD and ProfD, but the profile root itself is never generic TOOL data.
    for parent in roots.persistent_parents:
        profile = _child_root_for_path(target, parent)
        if profile is not None:
            for index, rule in enumerate(_FIREFOX_CACHE_RULES):
                _append_match(matches, normalized, profile, rule, index)
            _append_match(
                matches,
                normalized,
                profile,
                _FIREFOX_PERSISTENT_PROFILE_RULE,
                10_000,
            )
    for profile in roots.custom_profiles:
        for index, rule in enumerate(_FIREFOX_CACHE_RULES):
            _append_match(matches, normalized, profile, rule, index)
        _append_match(
            matches,
            normalized,
            profile,
            _FIREFOX_PERSISTENT_PROFILE_RULE,
            10_000,
        )

    for crash_root in roots.crash_roots:
        _append_match(matches, normalized, crash_root, _FIREFOX_CRASH_PENDING_RULE, 0)
        _append_match(matches, normalized, crash_root, _FIREFOX_CRASH_STATE_RULE, 100)

    for update_parent in roots.update_parents:
        for index, rule in enumerate(_FIREFOX_UPDATE_LOG_RULES):
            _append_match(matches, normalized, update_parent, rule, index)
        _append_match(matches, normalized, update_parent, _FIREFOX_UPDATE_STATE_RULE, 100)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def firefox_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = firefox_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()

    for parent in roots.local_parents:
        for profile in _existing_children(parent):
            _append_tool_root(found, seen, profile, _FIREFOX_LOCAL_PROFILE_RULE)
            for rule in _FIREFOX_CACHE_RULES:
                _append_tool_root(found, seen, profile, rule)
    for parent in roots.persistent_parents:
        for profile in _existing_children(parent):
            for rule in _FIREFOX_CACHE_RULES:
                _append_tool_root(found, seen, profile, rule)
    for profile in roots.custom_profiles:
        for rule in _FIREFOX_CACHE_RULES:
            _append_tool_root(found, seen, profile, rule)
    for crash_root in roots.crash_roots:
        _append_tool_root(found, seen, crash_root, _FIREFOX_CRASH_PENDING_RULE)
    return tuple(found)


def whole_tree_firefox_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in firefox_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_firefox_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_firefox_rule(path, environment)
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
        running = firefox_process_running()
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
def firefox_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'firefox.exe' -or $_.Name -ieq 'maintenanceservice.exe' -or "
        "($_.Name -ieq 'updater.exe' -and $_.CommandLine -match '(?i)mozilla|firefox') }; "
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


def clear_firefox_process_cache() -> None:
    firefox_process_running.cache_clear()


def _profiles_ini_paths(
    firefox_base: PureWindowsPath,
    localappdata: str | None,
) -> tuple[PureWindowsPath, ...]:
    ini = Path(str(firefox_base / "profiles.ini"))
    try:
        if not ini.is_file():
            return ()
        parser = configparser.RawConfigParser()
        parser.read(ini, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeError):
        return ()

    default_parent = firefox_base / "Profiles"
    local_parent = (
        PureWindowsPath(localappdata) / "Mozilla" / "Firefox" / "Profiles"
        if localappdata
        else None
    )
    found: list[PureWindowsPath] = []
    for section in parser.sections():
        path_text = parser.get(section, "Path", fallback="").strip()
        if not path_text:
            continue
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
        profile = firefox_base / path_text if is_relative else PureWindowsPath(path_text)
        # Default profiles already have dedicated persistent/local parent roots;
        # only keep custom/co-located profiles in this list.
        try:
            profile.relative_to(default_parent)
        except ValueError:
            found.append(profile)
            continue
        if local_parent is None:
            continue
    return _unique_paths(found)


def _append_msix_roots(
    localappdata: PureWindowsPath,
    persistent_parents: list[PureWindowsPath],
    local_parents: list[PureWindowsPath],
    crash_roots: list[PureWindowsPath],
) -> None:
    packages = Path(str(localappdata / "Packages"))
    try:
        children = tuple(packages.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir() or not child.name.casefold().startswith("mozilla.firefox_"):
            continue
        package = PureWindowsPath(str(child)) / "LocalCache"
        roaming = package / "Roaming" / "Mozilla" / "Firefox"
        local = package / "Local" / "Mozilla" / "Firefox"
        persistent_parents.append(roaming / "Profiles")
        local_parents.append(local / "Profiles")
        crash_roots.append(roaming / "Crash Reports")


def _child_root_for_path(
    path: PureWindowsPath, parent: PureWindowsPath
) -> PureWindowsPath | None:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return parent / relative.parts[0]


def _existing_children(parent: PureWindowsPath) -> tuple[PureWindowsPath, ...]:
    path = Path(str(parent))
    try:
        children = tuple(path.iterdir())
    except OSError:
        return ()
    return tuple(PureWindowsPath(str(child)) for child in children if child.is_dir())


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
    "FIREFOX_RULES",
    "FirefoxRootSet",
    "clear_firefox_process_cache",
    "evaluate_firefox_path",
    "firefox_audited_tool_roots",
    "firefox_process_running",
    "firefox_roots",
    "firefox_scan_roots",
    "match_firefox_rule",
    "whole_tree_firefox_rule",
]
