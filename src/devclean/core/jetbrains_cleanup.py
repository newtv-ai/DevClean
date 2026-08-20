"""Audited JetBrains IntelliJ-platform storage semantics for Windows cleanup.

JetBrains separates user configuration/plugins from a per-version system
directory. The system directory is mixed state: it contains regenerable indexes
and caches but also Local History and embedded-browser cookies. This profile
therefore keeps the system root protected and delegates only exact, source-backed
regenerable subtrees. Configuration and plugins are always persistent state.
"""

from __future__ import annotations

import ntpath
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

# Current/default Windows selectors documented by JetBrains products and their
# support knowledge base. Android Studio is intentionally excluded: it is a
# Google-distributed IntelliJ-platform product with its own storage lifecycle.
_SELECTOR_PREFIXES = (
    "IntelliJIdea",
    "IdeaIC",
    "PyCharm",
    "PyCharmCE",
    "WebStorm",
    "PhpStorm",
    "CLion",
    "DataGrip",
    "GoLand",
    "Rider",
    "RubyMine",
    "RustRover",
    "DataSpell",
    "Aqua",
    "MPS",
)
_SELECTOR_RE = re.compile(
    rf"^(?:{'|'.join(re.escape(item) for item in _SELECTOR_PREFIXES)})"
    r"\d{4}\.\d+(?:\.\d+)?$",
    re.IGNORECASE,
)

_PROPERTIES_ENV_KEYS = (
    "IDEA_PROPERTIES",
    "CLION_PROPERTIES",
    "PYCHARM_PROPERTIES",
    "RUBYMINE_PROPERTIES",
    "DATAGRIP_PROPERTIES",
    "WEBIDE_PROPERTIES",
    "PHPSTORM_PROPERTIES",
    "GOLAND_PROPERTIES",
    "RIDER_PROPERTIES",
    "RUSTROVER_PROPERTIES",
)
_PATH_PROPERTIES = {
    "idea.config.path": "config",
    "idea.system.path": "system",
    "idea.plugins.path": "plugins",
    "idea.log.path": "log",
}
_PROCESS_NAME_REGEX = (
    r"(?i)^(?:idea|pycharm|webstorm|phpstorm|clion|datagrip|goland|rider|"
    r"rubymine|rustrover|dataspell|aqua|mps)64?\.exe$"
)


@dataclass(frozen=True, slots=True)
class JetBrainsRootSet:
    config_roots: tuple[PureWindowsPath, ...]
    system_roots: tuple[PureWindowsPath, ...]
    plugin_roots: tuple[PureWindowsPath, ...]
    log_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative: str,
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
        app_id="jetbrains",
        root_key=root_key,
        relative_pattern=relative,
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


_JETBRAINS_INDEX_RULE = _rule(
    "jetbrains-index-cache",
    "index",
    DecisionOwner.TOOL,
    RebuildCost.HIGH,
    "JetBrains project indexes",
    root_key="JETBRAINS_SYSTEM",
    idle_days=30,
    min_reclaim_bytes=256 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)
_JETBRAINS_TMP_RULE = _rule(
    "jetbrains-system-temp",
    "tmp",
    DecisionOwner.TOOL,
    RebuildCost.LOW,
    "JetBrains system temporary files",
    root_key="JETBRAINS_SYSTEM",
    idle_days=7,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)
_JETBRAINS_VCS_LOG_RULE = _rule(
    "jetbrains-vcs-log-cache",
    "vcs-log",
    DecisionOwner.TOOL,
    RebuildCost.MEDIUM,
    "JetBrains VCS Log caches and indexes",
    root_key="JETBRAINS_SYSTEM",
    idle_days=30,
    min_reclaim_bytes=128 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)
_JETBRAINS_LOG_RULE = _rule(
    "jetbrains-product-logs",
    "",
    DecisionOwner.TOOL,
    RebuildCost.NONE,
    "JetBrains IDE product logs and thread dumps",
    root_key="JETBRAINS_LOG",
    idle_days=14,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)

_JETBRAINS_LOCAL_HISTORY_RULE = _rule(
    "jetbrains-local-history",
    "LocalHistory",
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "JetBrains Local History revisions",
    root_key="JETBRAINS_SYSTEM",
    user_age_buckets=(5, 30, 90),
)
_JETBRAINS_JCEF_RULE = _rule(
    "jetbrains-jcef-browser-state",
    "jcef_cache",
    DecisionOwner.USER,
    RebuildCost.MEDIUM,
    "JetBrains embedded-browser cache and cookies",
    root_key="JETBRAINS_SYSTEM",
    user_age_buckets=(30, 90, 180),
)
_JETBRAINS_VFS_RULE = _rule(
    "jetbrains-vfs-cache-state",
    "caches",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains virtual-file-system cache coupled to Local History invalidation",
    root_key="JETBRAINS_SYSTEM",
)
_JETBRAINS_SYSTEM_STATE_RULE = _rule(
    "jetbrains-system-state",
    "",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains mixed system state with caches and persistent Local History",
    root_key="JETBRAINS_SYSTEM",
)
_JETBRAINS_CONFIG_RULE = _rule(
    "jetbrains-config-state",
    "",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains user configuration, settings, templates and IDE state",
    root_key="JETBRAINS_CONFIG",
)
_JETBRAINS_PLUGIN_RULE = _rule(
    "jetbrains-user-plugins",
    "",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains user-installed plugins",
    root_key="JETBRAINS_PLUGINS",
)

JETBRAINS_RULES: tuple[ApplicationCleanupRule, ...] = (
    _JETBRAINS_INDEX_RULE,
    _JETBRAINS_TMP_RULE,
    _JETBRAINS_VCS_LOG_RULE,
    _JETBRAINS_LOG_RULE,
    _JETBRAINS_LOCAL_HISTORY_RULE,
    _JETBRAINS_JCEF_RULE,
    _JETBRAINS_VFS_RULE,
    _JETBRAINS_SYSTEM_STATE_RULE,
    _JETBRAINS_CONFIG_RULE,
    _JETBRAINS_PLUGIN_RULE,
)


def jetbrains_roots(
    environment: Mapping[str, str] | None = None,
) -> JetBrainsRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    userprofile = env.get("userprofile")

    configs: list[PureWindowsPath] = []
    systems: list[PureWindowsPath] = []
    plugins: list[PureWindowsPath] = []
    logs: list[PureWindowsPath] = []

    config_parent = PureWindowsPath(appdata) / "JetBrains" if appdata else None
    system_parent = PureWindowsPath(localappdata) / "JetBrains" if localappdata else None

    selectors = _existing_selectors(config_parent, system_parent)
    for selector in selectors:
        config = config_parent / selector if config_parent is not None else None
        system = system_parent / selector if system_parent is not None else None
        if config is not None:
            configs.append(config)
            plugins.append(config / "plugins")
        if system is not None:
            systems.append(system)
            logs.append(system / "log")

    explicit = {
        "config": env.get("devclean_jetbrains_config_dir"),
        "system": env.get("devclean_jetbrains_system_dir"),
        "plugins": env.get("devclean_jetbrains_plugins_dir"),
        "log": env.get("devclean_jetbrains_log_dir"),
    }
    _append_explicit_paths(explicit, configs, systems, plugins, logs)

    # Collect every source-backed property location rather than choosing one
    # global winner: separate installed products may legitimately use different
    # redirects. Stale/default locations remain protected by the broad fallbacks.
    if userprofile:
        _append_property_file(
            Path(str(PureWindowsPath(userprofile) / "idea.properties")),
            userprofile,
            configs,
            systems,
            plugins,
            logs,
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
    for key in _PROPERTIES_ENV_KEYS:
        value = env.get(key.casefold())
        if not value:
            continue
        candidate = PureWindowsPath(value)
        if not candidate.is_absolute():
            continue
        _append_property_file(
            Path(str(candidate)),
            userprofile,
            configs,
            systems,
            plugins,
            logs,
        )

    if environment is None:
        _append_explicit_paths(
            _running_override_paths(),
            configs,
            systems,
            plugins,
            logs,
        )

    # Every discovered config/system root has a documented default plugin/log
    # child unless an explicit override supplies an additional location.
    for config in tuple(configs):
        plugins.append(config / "plugins")
    for system in tuple(systems):
        logs.append(system / "log")

    return JetBrainsRootSet(
        config_roots=_unique_paths(configs),
        system_roots=_unique_paths(systems),
        plugin_roots=_unique_paths(plugins),
        log_roots=_unique_paths(logs),
    )


def jetbrains_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = jetbrains_roots(environment)
    return _minimal_roots(
        (
            *roots.config_roots,
            *roots.system_roots,
            *roots.plugin_roots,
            *roots.log_roots,
        )
    )


def match_jetbrains_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = jetbrains_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.system_roots:
        for index, rule in enumerate(
            (
                _JETBRAINS_INDEX_RULE,
                _JETBRAINS_TMP_RULE,
                _JETBRAINS_VCS_LOG_RULE,
                _JETBRAINS_LOCAL_HISTORY_RULE,
                _JETBRAINS_JCEF_RULE,
                _JETBRAINS_VFS_RULE,
            )
        ):
            _append_match(matches, normalized, root, rule, index)
        _append_match(matches, normalized, root, _JETBRAINS_SYSTEM_STATE_RULE, 10_000)

    for root in roots.config_roots:
        _append_match(matches, normalized, root, _JETBRAINS_CONFIG_RULE, 10_000)
    for root in roots.plugin_roots:
        _append_match(matches, normalized, root, _JETBRAINS_PLUGIN_RULE, 0)
    for root in roots.log_roots:
        _append_match(matches, normalized, root, _JETBRAINS_LOG_RULE, 0)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def jetbrains_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = jetbrains_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for root in roots.system_roots:
        for rule in (
            _JETBRAINS_INDEX_RULE,
            _JETBRAINS_TMP_RULE,
            _JETBRAINS_VCS_LOG_RULE,
        ):
            _append_tool_root(found, seen, root, rule)
    for root in roots.log_roots:
        _append_tool_root(found, seen, root, _JETBRAINS_LOG_RULE)
    return tuple(found)


def whole_tree_jetbrains_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in jetbrains_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_jetbrains_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_jetbrains_rule(path, environment)
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
        running = jetbrains_process_running()
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
def jetbrains_process_running() -> bool:
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


def clear_jetbrains_process_cache() -> None:
    jetbrains_process_running.cache_clear()
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
    "JETBRAINS_RULES",
    "JetBrainsRootSet",
    "clear_jetbrains_process_cache",
    "evaluate_jetbrains_path",
    "jetbrains_audited_tool_roots",
    "jetbrains_process_running",
    "jetbrains_roots",
    "jetbrains_scan_roots",
    "match_jetbrains_rule",
    "whole_tree_jetbrains_rule",
]
