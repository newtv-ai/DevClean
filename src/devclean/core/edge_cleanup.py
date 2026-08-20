"""Audited Microsoft Edge storage semantics for Windows cleanup.

Edge inherits Chromium profile/cache layout but has Microsoft-specific channel,
policy and updater locations. Browser profile state is never treated as a
cache merely because it lives beside regenerable Chromium data. The modern
Edge updater is also kept separate from browser cache semantics: diagnostic
logs are disposable, while updater binaries/state remain protected.
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
class EdgeRootSet:
    data_roots: tuple[PureWindowsPath, ...]
    disk_cache_roots: tuple[PureWindowsPath, ...]
    updater_roots: tuple[PureWindowsPath, ...]
    update_log_roots: tuple[PureWindowsPath, ...]


def _clone_chromium_rule(rule: ApplicationCleanupRule) -> ApplicationCleanupRule:
    root_map = {
        "CHROME_DATA": "EDGE_DATA",
        "CHROME_PROFILE": "EDGE_PROFILE",
        "CHROME_DISK_CACHE": "EDGE_DISK_CACHE",
    }
    return replace(
        rule,
        rule_id=rule.rule_id.replace("chrome-", "edge-", 1),
        app_id="edge",
        root_key=root_map[rule.root_key],
        label=rule.label.replace("Chrome", "Microsoft Edge"),
    )


_EDGE_CHROMIUM_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _clone_chromium_rule(rule)
    for rule in CHROME_RULES
    if rule.root_key in {"CHROME_DATA", "CHROME_PROFILE", "CHROME_DISK_CACHE"}
)

_EDGE_DATA_RULES = tuple(
    rule for rule in _EDGE_CHROMIUM_RULES if rule.root_key == "EDGE_DATA"
)
_EDGE_PROFILE_RULES = tuple(
    rule for rule in _EDGE_CHROMIUM_RULES if rule.root_key == "EDGE_PROFILE"
)
_EDGE_DISK_CACHE_RULE = next(
    rule for rule in _EDGE_CHROMIUM_RULES if rule.root_key == "EDGE_DISK_CACHE"
)

_EDGE_UPDATER_STATE_RULE = ApplicationCleanupRule(
    rule_id="edge-updater-state",
    app_id="edge",
    root_key="EDGE_UPDATER",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Microsoft Edge updater binaries and update state",
)

_EDGE_UPDATE_LOG_RULES: tuple[ApplicationCleanupRule, ...] = (
    ApplicationCleanupRule(
        rule_id="edge-update-log",
        app_id="edge",
        root_key="EDGE_UPDATE_LOG",
        relative_pattern="MicrosoftEdgeUpdate.log",
        match_kind=MatchKind.EXACT,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.NONE,
        idle_days=7,
        min_reclaim_bytes=256 * 1024,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Microsoft Edge Update diagnostic log",
    ),
    ApplicationCleanupRule(
        rule_id="edge-update-log-backup",
        app_id="edge",
        root_key="EDGE_UPDATE_LOG",
        relative_pattern="MicrosoftEdgeUpdate.log.bak",
        match_kind=MatchKind.EXACT,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.NONE,
        idle_days=7,
        min_reclaim_bytes=256 * 1024,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Rotated Microsoft Edge Update diagnostic log",
    ),
    ApplicationCleanupRule(
        rule_id="edge-installer-log",
        app_id="edge",
        root_key="EDGE_UPDATE_LOG",
        relative_pattern="msedge_installer.log",
        match_kind=MatchKind.EXACT,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.NONE,
        idle_days=7,
        min_reclaim_bytes=256 * 1024,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Microsoft Edge installer diagnostic log",
    ),
)

EDGE_RULES: tuple[ApplicationCleanupRule, ...] = (
    *_EDGE_CHROMIUM_RULES,
    *_EDGE_UPDATE_LOG_RULES,
    _EDGE_UPDATER_STATE_RULE,
)


def edge_roots(environment: Mapping[str, str] | None = None) -> EdgeRootSet:
    env = _casefold_env(environment)
    localappdata = env.get("localappdata")
    programfiles_x86 = env.get("programfiles(x86)") or env.get("programfilesx86")
    programdata = env.get("programdata") or env.get("allusersprofile")
    windir = env.get("windir")
    temp = env.get("temp") or env.get("tmp")

    data: list[PureWindowsPath] = []
    disk_cache: list[PureWindowsPath] = []
    updater: list[PureWindowsPath] = []
    update_logs: list[PureWindowsPath] = []

    if localappdata:
        base = PureWindowsPath(localappdata)
        data.extend(
            (
                base / "Microsoft" / "Edge" / "User Data",
                base / "Microsoft" / "Edge Beta" / "User Data",
                base / "Microsoft" / "Edge Dev" / "User Data",
                base / "Microsoft" / "Edge SxS" / "User Data",
            )
        )
        # Microsoft Support refers to this as the per-user Edge update cache,
        # but the directory can also hold updater state. Inventory it, never
        # grant raw whole-tree deletion authority.
        updater.append(base / "Microsoft" / "Edge" / "Update")
        update_logs.append(base / "Temp")
    if temp:
        update_logs.append(PureWindowsPath(temp))
    if programfiles_x86:
        updater.append(PureWindowsPath(programfiles_x86) / "Microsoft" / "EdgeUpdate")
    if programdata:
        machine_update = PureWindowsPath(programdata) / "Microsoft" / "EdgeUpdate"
        updater.append(machine_update)
        update_logs.append(machine_update / "Log")
    if windir:
        update_logs.append(PureWindowsPath(windir) / "Temp")

    explicit_data = env.get("devclean_edge_user_data_dir")
    explicit_cache = env.get("devclean_edge_disk_cache_dir")
    if explicit_data:
        data.insert(0, PureWindowsPath(explicit_data))
    if explicit_cache:
        disk_cache.insert(0, PureWindowsPath(explicit_cache))

    if environment is None:
        policy_data, policy_cache = _edge_policy_paths(env)
        if policy_data is not None:
            data.insert(0, policy_data)
        if policy_cache is not None:
            disk_cache.insert(0, policy_cache)
        running_data, running_cache = _running_override_roots()
        data[0:0] = running_data
        disk_cache[0:0] = running_cache

    return EdgeRootSet(
        data_roots=_unique_paths(data),
        disk_cache_roots=_unique_paths(disk_cache),
        updater_roots=_unique_paths(updater),
        update_log_roots=_unique_paths(update_logs),
    )


def edge_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = edge_roots(environment)
    # Shared Temp roots are intentionally excluded. Exact Edge log files can be
    # classified when generic temp scanning encounters them, but adding a shared
    # Temp directory as an application root would change semantics for unrelated
    # temporary files.
    return tuple(
        dict.fromkeys(
            (
                *roots.data_roots,
                *roots.disk_cache_roots,
                *roots.updater_roots,
            )
        )
    )


def match_edge_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = edge_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.disk_cache_roots:
        _append_match(matches, normalized, root, _EDGE_DISK_CACHE_RULE, 0)

    for root in roots.update_log_roots:
        for index, rule in enumerate(_EDGE_UPDATE_LOG_RULES):
            _append_match(matches, normalized, root, rule, index)

    for root in roots.updater_roots:
        _append_match(matches, normalized, root, _EDGE_UPDATER_STATE_RULE, 0)

    for root in roots.data_roots:
        profile_root = _profile_root_for_path(path, root)
        if profile_root is not None:
            for index, rule in enumerate(_EDGE_PROFILE_RULES):
                _append_match(matches, normalized, profile_root, rule, index)
        for index, rule in enumerate(_EDGE_DATA_RULES):
            _append_match(matches, normalized, root, rule, index)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def edge_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = edge_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()

    for root in roots.data_roots:
        for rule in _EDGE_DATA_RULES:
            _append_tool_root(found, seen, root, rule)
        for profile_root in _existing_profile_roots(root):
            for rule in _EDGE_PROFILE_RULES:
                _append_tool_root(found, seen, profile_root, rule)
    for root in roots.disk_cache_roots:
        _append_tool_root(found, seen, root, _EDGE_DISK_CACHE_RULE)
    return tuple(found)


def whole_tree_edge_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in edge_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_edge_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_edge_rule(path, environment)
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
        running = edge_process_running()
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
def edge_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'msedge.exe' -or $_.Name -ieq 'MicrosoftEdgeUpdate.exe' }; "
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
    tuple[PureWindowsPath, ...], tuple[PureWindowsPath, ...]
]:
    if os.name != "nt":
        return (), ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'msedge.exe' }; "
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


def _edge_policy_paths(
    env: Mapping[str, str],
) -> tuple[PureWindowsPath | None, PureWindowsPath | None]:
    if os.name != "nt":
        return None, None
    try:
        import winreg
    except ImportError:
        return None, None

    values: dict[str, str] = {}
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Policies\Microsoft\Edge") as key:
                for name in ("UserDataDir", "DiskCacheDir"):
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                    except OSError:
                        continue
                    supported_kind = kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                    if supported_kind and isinstance(value, str):
                        values.setdefault(name, value)
        except OSError:
            continue

    return (
        _policy_path(values.get("UserDataDir"), env),
        _policy_path(values.get("DiskCacheDir"), env),
    )


def _replace_policy_token(text: str, key: str, replacement: str) -> str:
    pattern = re.compile(rf"\$\{{{re.escape(key)}\}}", re.IGNORECASE)

    def literal_replacement(_match: re.Match[str]) -> str:
        return replacement

    return pattern.sub(literal_replacement, text)


def _policy_path(value: str | None, env: Mapping[str, str]) -> PureWindowsPath | None:
    if not value:
        return None
    profile = env.get("userprofile", "")
    localappdata = env.get("localappdata", "")
    profile_path = PureWindowsPath(profile) if profile else None
    mapping = {
        "local_app_data": localappdata,
        "user_home": profile,
        "profile": profile,
        "users": str(profile_path.parent) if profile_path is not None else "",
        "user_name": profile_path.name if profile_path is not None else "",
    }
    expanded = value
    for key, replacement in mapping.items():
        if replacement:
            expanded = _replace_policy_token(expanded, key, replacement)
    if re.search(r"\$\{[^}]+\}|%[^%]+%", expanded):
        return None
    path = PureWindowsPath(expanded)
    return path if path.is_absolute() else None


def clear_edge_process_cache() -> None:
    edge_process_running.cache_clear()
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
    return (
        lowered in {"default", "guest profile", "system profile"}
        or lowered.startswith("profile ")
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
    "EDGE_RULES",
    "EdgeRootSet",
    "clear_edge_process_cache",
    "edge_audited_tool_roots",
    "edge_process_running",
    "edge_roots",
    "edge_scan_roots",
    "evaluate_edge_path",
    "match_edge_rule",
    "whole_tree_edge_rule",
]
