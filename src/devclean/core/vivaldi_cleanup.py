"""Audited Vivaldi browser storage semantics for Windows cleanup.

Vivaldi uses Chromium profile/cache storage but supports a Windows standalone
installation whose User Data can live beside the application. Chromium cache
rules are cloned from the already-audited Chrome profile so browser safety
corrections stay aligned. Vivaldi profile data remains authoritative; proven
regenerable Chromium caches keep their existing policy while Crashpad reports
remain visible diagnostic evidence without generic raw deletion authority.
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


@dataclass(frozen=True, slots=True)
class VivaldiRootSet:
    data_roots: tuple[PureWindowsPath, ...]
    disk_cache_roots: tuple[PureWindowsPath, ...]


def _clone_chromium_rule(rule: ApplicationCleanupRule) -> ApplicationCleanupRule:
    root_map = {
        "CHROME_DATA": "VIVALDI_DATA",
        "CHROME_PROFILE": "VIVALDI_PROFILE",
        "CHROME_DISK_CACHE": "VIVALDI_DISK_CACHE",
    }
    return replace(
        rule,
        rule_id=rule.rule_id.replace("chrome-", "vivaldi-", 1),
        app_id="vivaldi",
        root_key=root_map[rule.root_key],
        label=rule.label.replace("Chrome", "Vivaldi"),
    )


_VIVALDI_CHROMIUM_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _clone_chromium_rule(rule)
    for rule in CHROME_RULES
    if rule.root_key in {"CHROME_DATA", "CHROME_PROFILE", "CHROME_DISK_CACHE"}
)
_VIVALDI_DATA_RULES = tuple(
    rule for rule in _VIVALDI_CHROMIUM_RULES if rule.root_key == "VIVALDI_DATA"
)
_VIVALDI_PROFILE_RULES = tuple(
    rule for rule in _VIVALDI_CHROMIUM_RULES if rule.root_key == "VIVALDI_PROFILE"
)
_VIVALDI_DISK_CACHE_RULE = next(
    rule for rule in _VIVALDI_CHROMIUM_RULES if rule.root_key == "VIVALDI_DISK_CACHE"
)

# Vivaldi documents this exact Windows location specifically so users can find
# and submit crash dumps. These reports are diagnostic evidence, not cache.
# Current Crashpad also owns a report-database prune lifecycle; DevClean must not
# replace it with an unrelated seven-day whole-tree rule.
_VIVALDI_CRASHPAD_REPORTS_RULE = ApplicationCleanupRule(
    rule_id="vivaldi-crashpad-reports",
    app_id="vivaldi",
    root_key="VIVALDI_DATA",
    relative_pattern=r"Crashpad\reports",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.NONE,
    label="Vivaldi Crashpad diagnostic reports retained for crash investigation",
)

VIVALDI_RULES: tuple[ApplicationCleanupRule, ...] = (
    *_VIVALDI_CHROMIUM_RULES,
    _VIVALDI_CRASHPAD_REPORTS_RULE,
)


def vivaldi_roots(environment: Mapping[str, str] | None = None) -> VivaldiRootSet:
    env = _casefold_env(environment)
    localappdata = env.get("localappdata")

    data: list[PureWindowsPath] = []
    disk_cache: list[PureWindowsPath] = []

    if localappdata:
        data.append(PureWindowsPath(localappdata) / "Vivaldi" / "User Data")

    # DevClean-only explicit overrides for tests and unusual installations.
    explicit_data = env.get("devclean_vivaldi_user_data_dir")
    explicit_cache = env.get("devclean_vivaldi_disk_cache_dir")
    if explicit_data:
        data.insert(0, PureWindowsPath(explicit_data))
    if explicit_cache:
        disk_cache.insert(0, PureWindowsPath(explicit_cache))

    if environment is None:
        running_data, running_cache = _running_override_roots()
        data[0:0] = running_data
        disk_cache[0:0] = running_cache

    return VivaldiRootSet(
        data_roots=_unique_paths(data),
        disk_cache_roots=_unique_paths(disk_cache),
    )


def vivaldi_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = vivaldi_roots(environment)
    return tuple(dict.fromkeys((*roots.data_roots, *roots.disk_cache_roots)))


def match_vivaldi_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = vivaldi_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.disk_cache_roots:
        _append_match(matches, normalized, root, _VIVALDI_DISK_CACHE_RULE, 0)

    for root in roots.data_roots:
        _append_match(matches, normalized, root, _VIVALDI_CRASHPAD_REPORTS_RULE, 0)
        profile_root = _profile_root_for_path(path, root)
        if profile_root is not None:
            for index, rule in enumerate(_VIVALDI_PROFILE_RULES):
                _append_match(matches, normalized, profile_root, rule, index)
        for index, rule in enumerate(_VIVALDI_DATA_RULES):
            _append_match(matches, normalized, root, rule, index)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def vivaldi_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = vivaldi_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()

    for root in roots.data_roots:
        for rule in _VIVALDI_DATA_RULES:
            _append_tool_root(found, seen, root, rule)
        for profile_root in _existing_profile_roots(root):
            for rule in _VIVALDI_PROFILE_RULES:
                _append_tool_root(found, seen, profile_root, rule)
    for root in roots.disk_cache_roots:
        _append_tool_root(found, seen, root, _VIVALDI_DISK_CACHE_RULE)
    return tuple(found)


def whole_tree_vivaldi_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in vivaldi_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_vivaldi_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_vivaldi_rule(path, environment)
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
        running = vivaldi_process_running()
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
def vivaldi_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'vivaldi.exe' }; if ($p) { 'RUNNING' }"
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
        "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'vivaldi.exe' }; "
        '$p | ForEach-Object { "{0}`t{1}" -f $_.ExecutablePath,$_.CommandLine }'
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
        executable, _, command_line = line.partition("\t")
        value = _switch_path(command_line, "user-data-dir")
        if value:
            data.append(PureWindowsPath(value))
        elif executable:
            standalone = _standalone_user_data_root(executable)
            if standalone is not None:
                data.append(standalone)
        value = _switch_path(command_line, "disk-cache-dir")
        if value:
            cache.append(PureWindowsPath(value))
    return _unique_paths(data), _unique_paths(cache)


def _standalone_user_data_root(executable: str) -> PureWindowsPath | None:
    exe = PureWindowsPath(executable)
    if exe.parent.name.casefold() != "application":
        return None
    candidate = exe.parent.parent / "User Data"
    try:
        if not Path(str(candidate)).is_dir():
            return None
    except OSError:
        return None
    return candidate


def clear_vivaldi_process_cache() -> None:
    vivaldi_process_running.cache_clear()
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
    "VIVALDI_RULES",
    "VivaldiRootSet",
    "clear_vivaldi_process_cache",
    "evaluate_vivaldi_path",
    "match_vivaldi_rule",
    "vivaldi_audited_tool_roots",
    "vivaldi_process_running",
    "vivaldi_roots",
    "vivaldi_scan_roots",
    "whole_tree_vivaldi_rule",
]
