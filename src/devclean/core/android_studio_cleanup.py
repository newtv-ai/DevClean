"""Audited Android Studio storage semantics for Windows cleanup.

Android Studio is IntelliJ-platform based, but Google owns its Windows storage
layout. Configuration/plugins live under roaming AppData while the mixed system
directory lives under local AppData. The mixed system root contains regenerable
platform caches alongside Local History and embedded-browser state, so only exact
audited cache/log subtrees receive TOOL whole-tree authority.
"""

from __future__ import annotations

import ntpath
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
    PolicyAction,
    effective_idle_days,
)
from devclean.core.jetbrains_cleanup import JETBRAINS_RULES

_SELECTOR_RE = re.compile(
    r"^AndroidStudio(?:Preview)?\d+(?:\.\d+){1,3}$",
    re.IGNORECASE,
)
_PATH_PROPERTIES = {
    "idea.config.path": "config",
    "idea.system.path": "system",
    "idea.plugins.path": "plugins",
    "idea.log.path": "log",
}
_PROCESS_NAME_REGEX = r"(?i)^studio64?\.exe$"
_ROOT_KEY_MAP = {
    "JETBRAINS_CONFIG": "ANDROID_STUDIO_CONFIG",
    "JETBRAINS_SYSTEM": "ANDROID_STUDIO_SYSTEM",
    "JETBRAINS_PLUGINS": "ANDROID_STUDIO_PLUGINS",
    "JETBRAINS_LOG": "ANDROID_STUDIO_LOG",
}


@dataclass(frozen=True, slots=True)
class AndroidStudioRootSet:
    config_roots: tuple[PureWindowsPath, ...]
    system_roots: tuple[PureWindowsPath, ...]
    plugin_roots: tuple[PureWindowsPath, ...]
    log_roots: tuple[PureWindowsPath, ...]


def _clone_platform_rule(rule: ApplicationCleanupRule) -> ApplicationCleanupRule:
    root_key = _ROOT_KEY_MAP.get(rule.root_key)
    if root_key is None:
        raise ValueError(f"unsupported JetBrains platform root key: {rule.root_key}")
    return replace(
        rule,
        rule_id=rule.rule_id.replace("jetbrains-", "android-studio-", 1),
        app_id="android_studio",
        root_key=root_key,
        label=rule.label.replace("JetBrains", "Android Studio"),
    )


ANDROID_STUDIO_RULES: tuple[ApplicationCleanupRule, ...] = tuple(
    _clone_platform_rule(rule) for rule in JETBRAINS_RULES
)
_RULE_BY_ID = {rule.rule_id: rule for rule in ANDROID_STUDIO_RULES}
_ANDROID_STUDIO_INDEX_RULE = _RULE_BY_ID["android-studio-index-cache"]
_ANDROID_STUDIO_TMP_RULE = _RULE_BY_ID["android-studio-system-temp"]
_ANDROID_STUDIO_VCS_LOG_RULE = _RULE_BY_ID["android-studio-vcs-log-cache"]
_ANDROID_STUDIO_LOG_RULE = _RULE_BY_ID["android-studio-product-logs"]
_ANDROID_STUDIO_LOCAL_HISTORY_RULE = _RULE_BY_ID["android-studio-local-history"]
_ANDROID_STUDIO_JCEF_RULE = _RULE_BY_ID["android-studio-jcef-browser-state"]
_ANDROID_STUDIO_VFS_RULE = _RULE_BY_ID["android-studio-vfs-cache-state"]
_ANDROID_STUDIO_SYSTEM_STATE_RULE = _RULE_BY_ID["android-studio-system-state"]
_ANDROID_STUDIO_CONFIG_RULE = _RULE_BY_ID["android-studio-config-state"]
_ANDROID_STUDIO_PLUGIN_RULE = _RULE_BY_ID["android-studio-user-plugins"]


def android_studio_roots(
    environment: Mapping[str, str] | None = None,
) -> AndroidStudioRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    userprofile = env.get("userprofile")

    configs: list[PureWindowsPath] = []
    systems: list[PureWindowsPath] = []
    plugins: list[PureWindowsPath] = []
    logs: list[PureWindowsPath] = []

    config_parent = PureWindowsPath(appdata) / "Google" if appdata else None
    system_parent = PureWindowsPath(localappdata) / "Google" if localappdata else None
    for selector in _existing_selectors(config_parent, system_parent):
        config = config_parent / selector if config_parent is not None else None
        system = system_parent / selector if system_parent is not None else None
        if config is not None:
            configs.append(config)
            plugins.append(config / "plugins")
        if system is not None:
            systems.append(system)
            logs.append(system / "log")

    _append_explicit_paths(
        {
            "config": env.get("devclean_android_studio_config_dir"),
            "system": env.get("devclean_android_studio_system_dir"),
            "plugins": env.get("devclean_android_studio_plugins_dir"),
            "log": env.get("devclean_android_studio_log_dir"),
        },
        configs,
        systems,
        plugins,
        logs,
    )

    # Android Studio documents STUDIO_PROPERTIES as the override for the
    # idea.properties file. Per-install config idea.properties files are also
    # source-backed and may redirect the standard IntelliJ path properties.
    studio_properties = env.get("studio_properties")
    if studio_properties:
        candidate = PureWindowsPath(studio_properties)
        if candidate.is_absolute():
            _append_property_file(
                Path(str(candidate)), userprofile, configs, systems, plugins, logs
            )
    for config in tuple(configs):
        _append_property_file(
            Path(str(config / "idea.properties")),
            userprofile,
            configs,
            systems,
            plugins,
            logs,
        )

    if environment is None:
        _append_explicit_paths(_running_override_paths(), configs, systems, plugins, logs)

    for config in tuple(configs):
        plugins.append(config / "plugins")
    for system in tuple(systems):
        logs.append(system / "log")

    return AndroidStudioRootSet(
        config_roots=_unique_paths(configs),
        system_roots=_unique_paths(systems),
        plugin_roots=_unique_paths(plugins),
        log_roots=_unique_paths(logs),
    )


def android_studio_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = android_studio_roots(environment)
    return _minimal_roots(
        (
            *roots.config_roots,
            *roots.system_roots,
            *roots.plugin_roots,
            *roots.log_roots,
        )
    )


def match_android_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = android_studio_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.system_roots:
        for index, rule in enumerate(
            (
                _ANDROID_STUDIO_INDEX_RULE,
                _ANDROID_STUDIO_TMP_RULE,
                _ANDROID_STUDIO_VCS_LOG_RULE,
                _ANDROID_STUDIO_LOCAL_HISTORY_RULE,
                _ANDROID_STUDIO_JCEF_RULE,
                _ANDROID_STUDIO_VFS_RULE,
            )
        ):
            _append_match(matches, normalized, root, rule, index)
        _append_match(
            matches,
            normalized,
            root,
            _ANDROID_STUDIO_SYSTEM_STATE_RULE,
            10_000,
        )

    for root in roots.config_roots:
        _append_match(matches, normalized, root, _ANDROID_STUDIO_CONFIG_RULE, 10_000)
    for root in roots.plugin_roots:
        _append_match(matches, normalized, root, _ANDROID_STUDIO_PLUGIN_RULE, 0)
    for root in roots.log_roots:
        _append_match(matches, normalized, root, _ANDROID_STUDIO_LOG_RULE, 0)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def android_studio_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = android_studio_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for root in roots.system_roots:
        for rule in (
            _ANDROID_STUDIO_INDEX_RULE,
            _ANDROID_STUDIO_TMP_RULE,
            _ANDROID_STUDIO_VCS_LOG_RULE,
        ):
            _append_tool_root(found, seen, root, rule)
    for root in roots.log_roots:
        _append_tool_root(found, seen, root, _ANDROID_STUDIO_LOG_RULE)
    return tuple(found)


def whole_tree_android_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in android_studio_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_android_studio_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_android_studio_rule(path, environment)
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
        running = android_studio_process_running()
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
def android_studio_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        f"$_.Name -match '{_PROCESS_NAME_REGEX}' }}; "
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
def _running_override_paths() -> dict[str, str]:
    if os.name != "nt":
        return {}
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        f"$_.Name -match '{_PROCESS_NAME_REGEX}' }}; "
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
        return {}
    if result.returncode != 0:
        return {}

    found: dict[str, str] = {}
    for line in result.stdout.splitlines():
        for property_name, kind in _PATH_PROPERTIES.items():
            value = _vm_property(line, property_name)
            if value and PureWindowsPath(value).is_absolute():
                found[kind] = value
    return found


def clear_android_studio_process_cache() -> None:
    android_studio_process_running.cache_clear()
    _running_override_paths.cache_clear()


def _existing_selectors(
    config_parent: PureWindowsPath | None,
    system_parent: PureWindowsPath | None,
) -> tuple[str, ...]:
    found: dict[str, str] = {}
    for parent in (config_parent, system_parent):
        if parent is None:
            continue
        try:
            children = tuple(Path(str(parent)).iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or not _SELECTOR_RE.fullmatch(child.name):
                continue
            found.setdefault(child.name.casefold(), child.name)
    return tuple(found.values())


def _append_property_file(
    path: Path,
    userprofile: str | None,
    configs: list[PureWindowsPath],
    systems: list[PureWindowsPath],
    plugins: list[PureWindowsPath],
    logs: list[PureWindowsPath],
) -> None:
    properties = _read_path_properties(path, userprofile)
    _append_explicit_paths(
        {kind: properties.get(property_name) for property_name, kind in _PATH_PROPERTIES.items()},
        configs,
        systems,
        plugins,
        logs,
    )


def _read_path_properties(path: Path, userprofile: str | None) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _PATH_PROPERTIES:
            continue
        rendered = _expand_property_path(value.strip(), userprofile)
        if rendered is not None:
            values[key] = rendered
    return values


def _expand_property_path(value: str, userprofile: str | None) -> str | None:
    rendered = value.strip().strip('"').strip("'")
    if userprofile:
        rendered = rendered.replace("${user.home}", userprofile)
    if "${" in rendered or "%" in rendered:
        return None
    normalized = ntpath.normpath(rendered.replace("/", "\\"))
    candidate = PureWindowsPath(normalized)
    return str(candidate) if candidate.is_absolute() else None


def _vm_property(command_line: str, property_name: str) -> str | None:
    pattern = re.compile(
        rf"""(?:^|\s)-D{re.escape(property_name)}=(?:"([^"]+)"|'([^']+)'|([^\s]+))""",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


def _append_explicit_paths(
    values: Mapping[str, str | None],
    configs: list[PureWindowsPath],
    systems: list[PureWindowsPath],
    plugins: list[PureWindowsPath],
    logs: list[PureWindowsPath],
) -> None:
    targets = {
        "config": configs,
        "system": systems,
        "plugins": plugins,
        "log": logs,
    }
    for kind, value in values.items():
        if not value or kind not in targets:
            continue
        candidate = PureWindowsPath(value)
        if candidate.is_absolute():
            targets[kind].append(candidate)


def _append_match(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
    index: int,
) -> None:
    normalized_root = _impl._normalize(root)
    relative = rule.relative_pattern
    candidate = normalized_root + ("\\" + relative if relative else "")
    if not _impl._matches(normalized_path, candidate, rule.match_kind):
        return
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


def _minimal_roots(paths: tuple[PureWindowsPath, ...]) -> tuple[PureWindowsPath, ...]:
    ordered = sorted(_unique_paths(list(paths)), key=lambda item: len(item.parts))
    found: list[PureWindowsPath] = []
    for path in ordered:
        normalized = _impl._normalize(path)
        if any(
            normalized == _impl._normalize(parent)
            or normalized.startswith(_impl._normalize(parent) + "\\")
            for parent in found
        ):
            continue
        found.append(path)
    return tuple(found)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "ANDROID_STUDIO_RULES",
    "AndroidStudioRootSet",
    "android_studio_audited_tool_roots",
    "android_studio_process_running",
    "android_studio_roots",
    "android_studio_scan_roots",
    "clear_android_studio_process_cache",
    "evaluate_android_studio_path",
    "match_android_studio_rule",
    "whole_tree_android_studio_rule",
]
