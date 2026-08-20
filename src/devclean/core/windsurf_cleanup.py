"""Audited Windsurf storage semantics for Windows cleanup.

Windsurf is VS Code OSS-derived, so its editor profile contains familiar
Electron caches beside unique workspace/history/recovery state. Windsurf also
keeps Cascade conversations, memories, plans and authored customizations in
hidden home directories. Only proven regenerable cache subtrees are TOOL-owned;
all Cascade/user-authored state is USER or KEEP.
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

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class WindsurfRootSet:
    data_roots: tuple[PureWindowsPath, ...]
    extension_roots: tuple[PureWindowsPath, ...]
    config_roots: tuple[PureWindowsPath, ...]
    plan_roots: tuple[PureWindowsPath, ...]
    system_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_kind: str = "data",
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    user_age_buckets: tuple[int, ...] = (),
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="windsurf",
        root_key=f"WINDSURF_{root_kind.upper()}",
        relative_pattern=relative_pattern,
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        user_age_buckets=user_age_buckets,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


def _tool_dir(
    rule_id: str,
    relative: str,
    label: str,
    *,
    idle_days: float = 7,
    min_reclaim_bytes: int = 4 * _MIB,
    rebuild_cost: RebuildCost = RebuildCost.LOW,
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


WINDSURF_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_dir("windsurf-cache", "Cache", "Windsurf Chromium resource cache"),
    _tool_dir("windsurf-cached-data", "CachedData", "Windsurf cached application data"),
    _tool_dir(
        "windsurf-cached-configurations",
        "CachedConfigurations",
        "Windsurf cached configuration metadata",
    ),
    _tool_dir(
        "windsurf-cached-profiles",
        "CachedProfilesData",
        "Windsurf cached profile metadata",
    ),
    _tool_dir(
        "windsurf-cached-extensions",
        "CachedExtensions",
        "Windsurf extension metadata cache",
    ),
    _tool_dir("windsurf-code-cache", "Code Cache", "Windsurf Chromium code cache"),
    _tool_dir("windsurf-gpu-cache", "GPUCache", "Windsurf GPU cache", idle_days=3),
    _tool_dir("windsurf-dawn-cache", "DawnCache", "Windsurf WebGPU/Dawn cache", idle_days=3),
    _tool_dir(
        "windsurf-grshader-cache",
        "GrShaderCache",
        "Windsurf graphics shader cache",
        idle_days=3,
    ),
    _tool_dir(
        "windsurf-shader-cache",
        "ShaderCache",
        "Windsurf graphics shader cache",
        idle_days=3,
    ),
    _rule(
        "windsurf-site-cache-storage",
        r"Service Worker\CacheStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf persistent Cache Storage / offline webview data",
        user_age_buckets=(30, 90, 180),
    ),
    _tool_dir(
        "windsurf-service-worker-script-cache",
        r"Service Worker\ScriptCache",
        "Windsurf service-worker script cache",
    ),
    _tool_dir(
        "windsurf-extension-vsix-cache",
        "CachedExtensionVSIXs",
        "Windsurf downloaded extension package cache",
        idle_days=14,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_dir(
        "windsurf-logs",
        "logs",
        "Windsurf diagnostic logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "windsurf-crashpad-reports",
        r"Crashpad\reports",
        "Windsurf Crashpad reports",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "windsurf-crashpad-pending",
        r"Crashpad\pending",
        "Windsurf pending crash reports",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _rule(
        "windsurf-workspace-state",
        r"User\workspaceStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf workspace state and local editor/chat metadata",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "windsurf-local-history",
        r"User\History",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf local file history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "windsurf-hot-exit-backups",
        "Backups",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf unsaved editor / hot-exit recovery data",
    ),
    _rule(
        "windsurf-user-state",
        "User",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf settings, profiles, extension state and global storage",
    ),
    _rule(
        "windsurf-service-worker-other-state",
        "Service Worker",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Windsurf service-worker persistent state",
    ),
    _rule(
        "windsurf-editor-unknown-state",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Windsurf editor state",
    ),
    _rule(
        "windsurf-cascade-state",
        "cascade",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf Cascade conversations and local settings",
        root_kind="config",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "windsurf-global-rules",
        r"memories\global_rules.md",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf user-authored global rules",
        root_kind="config",
    ),
    _rule(
        "windsurf-memories",
        "memories",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf locally stored Cascade memories",
        root_kind="config",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "windsurf-mcp-config",
        "mcp_config.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf MCP configuration and credentials",
        root_kind="config",
    ),
    _rule(
        "windsurf-hooks",
        "hooks.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf user hooks",
        root_kind="config",
    ),
    _rule(
        "windsurf-global-workflows",
        "global_workflows",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf user-authored global workflows",
        root_kind="config",
    ),
    _rule(
        "windsurf-global-skills",
        "skills",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf user-authored global skills",
        root_kind="config",
    ),
    _rule(
        "windsurf-config-unknown-state",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Windsurf configuration and Cascade state",
        root_kind="config",
    ),
    _rule(
        "windsurf-plans",
        "",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Windsurf Plan-mode persistent plan files",
        root_kind="plans",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "windsurf-installed-extensions",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf installed extensions",
        root_kind="extensions",
    ),
    _rule(
        "windsurf-system-policy",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Windsurf enterprise system rules, workflows, skills and hooks",
        root_kind="system",
    ),
)


def windsurf_roots(environment: Mapping[str, str] | None = None) -> WindsurfRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    profile = env.get("userprofile")
    programdata = env.get("programdata")
    data: list[PureWindowsPath] = []
    extensions: list[PureWindowsPath] = []
    config: list[PureWindowsPath] = []
    plans: list[PureWindowsPath] = []
    system: list[PureWindowsPath] = []

    if environment is None:
        running_data, running_extensions = _running_override_roots()
        data.extend(running_data)
        extensions.extend(running_extensions)

    if appdata:
        # Windsurf is a VS Code OSS fork. These are traversal anchors only;
        # generic deletion is still denied unless a specific child rule matches.
        data.extend(
            (
                PureWindowsPath(appdata) / "Windsurf",
                PureWindowsPath(appdata) / "Windsurf - Next",
            )
        )
    if profile:
        home = PureWindowsPath(profile)
        config.append(home / ".codeium" / "windsurf")
        plans.append(home / ".windsurf" / "plans")
        extensions.append(home / ".windsurf" / "extensions")
    if programdata:
        system.append(PureWindowsPath(programdata) / "Windsurf")

    return WindsurfRootSet(
        data_roots=_unique_paths(data),
        extension_roots=_unique_paths(extensions),
        config_roots=_unique_paths(config),
        plan_roots=_unique_paths(plans),
        system_roots=_unique_paths(system),
    )


def windsurf_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = windsurf_roots(environment)
    return (*roots.data_roots, *roots.config_roots, *roots.plan_roots)


def match_windsurf_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = windsurf_roots(environment)
    groups = {
        "WINDSURF_DATA": roots.data_roots,
        "WINDSURF_EXTENSIONS": roots.extension_roots,
        "WINDSURF_CONFIG": roots.config_roots,
        "WINDSURF_PLANS": roots.plan_roots,
        "WINDSURF_SYSTEM": roots.system_roots,
    }
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(WINDSURF_RULES):
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
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def windsurf_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = windsurf_roots(environment)
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for rule in WINDSURF_RULES:
        if (
            rule.owner is not DecisionOwner.TOOL
            or not rule.allow_whole_tree
            or rule.root_key != "WINDSURF_DATA"
        ):
            continue
        if any(token in rule.relative_pattern for token in ("*", "?", "[", "{")):
            continue
        for root in roots.data_roots:
            path = root / rule.relative_pattern if rule.relative_pattern else root
            key = _impl._normalize(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((path, rule))
    return tuple(found)


def whole_tree_windsurf_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in windsurf_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_windsurf_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_windsurf_rule(path, environment)
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
        running = windsurf_process_running()
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
def windsurf_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -like 'Windsurf*.exe' }; if ($p) { 'RUNNING' }"
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
        "$_.Name -like 'Windsurf*.exe' }; $p | ForEach-Object { $_.CommandLine }"
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
    extensions: list[PureWindowsPath] = []
    for line in result.stdout.splitlines():
        user_data = _argument_value(line, "--user-data-dir")
        extension_dir = _argument_value(line, "--extensions-dir")
        if user_data:
            data.append(PureWindowsPath(user_data))
        if extension_dir:
            extensions.append(PureWindowsPath(extension_dir))
    return _unique_paths(data), _unique_paths(extensions)


def _argument_value(command_line: str, flag: str) -> str | None:
    pattern = re.compile(
        rf"(?:^|\s){re.escape(flag)}(?:=|\s+)(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>[^\s]+))",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if match is None:
        return None
    return match.group("quoted") or match.group("bare")


def clear_windsurf_process_cache() -> None:
    windsurf_process_running.cache_clear()
    _running_override_roots.cache_clear()


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold().rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return tuple(found)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "WINDSURF_RULES",
    "WindsurfRootSet",
    "clear_windsurf_process_cache",
    "evaluate_windsurf_path",
    "match_windsurf_rule",
    "whole_tree_windsurf_rule",
    "windsurf_audited_tool_roots",
    "windsurf_process_running",
    "windsurf_roots",
    "windsurf_scan_roots",
]
