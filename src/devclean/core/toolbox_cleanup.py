"""Audited JetBrains Toolbox App storage semantics for Windows cleanup.

The Toolbox App keeps executable/configuration state and disposable storage under
one Windows root. This profile therefore protects the root by default and grants
whole-tree cleanup authority only to exact vendor-documented removable subtrees.
Tool installations and rollback copies live outside this root and are deliberately
not inferred from directory names: Toolbox 3.6 exposes those through its own
storage manager, where active/previous/leftover identity is authoritative.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
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


def _rule(
    rule_id: str,
    relative: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="toolbox",
        root_key="TOOLBOX_ROOT",
        relative_pattern=relative,
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


# JetBrains documents cache/download as a three-day download cache. Preserve the
# vendor retention floor even for very large payloads instead of shortening it
# through DevClean's generic size-sensitive idle heuristic.
_TOOLBOX_DOWNLOAD_CACHE_RULE = _rule(
    "toolbox-download-cache",
    r"cache\download",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.LOW,
    "JetBrains Toolbox downloaded IDE package cache",
    idle_days=3,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
)

# Failed/unpacked installs can leave cache/temp behind. JetBrains explicitly
# describes old temp entries as removable, but does not publish a fixed TTL, so
# DevClean uses a conservative seven-day idle floor.
_TOOLBOX_TEMP_RULE = _rule(
    "toolbox-install-temp",
    r"cache\temp",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.LOW,
    "JetBrains Toolbox installation temporary leftovers",
    idle_days=7,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
)
_TOOLBOX_LOG_RULE = _rule(
    "toolbox-product-logs",
    "logs",
    MatchKind.PREFIX,
    DecisionOwner.TOOL,
    RebuildCost.NONE,
    "JetBrains Toolbox diagnostic logs",
    idle_days=14,
    min_reclaim_bytes=8 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
)

# These children are state, executable code, IPC coordination, or user/plugin
# material. They stay explicitly protected even though some live below a folder
# named cache. The root fallback protects every unknown/new Toolbox child too.
_TOOLBOX_SETTINGS_RULE = _rule(
    "toolbox-settings-state",
    ".settings.json",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox settings and network configuration",
)
_TOOLBOX_ENVIRONMENT_RULE = _rule(
    "toolbox-environment-state",
    "environment.json",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox environment snapshot",
)
_TOOLBOX_BIN_RULE = _rule(
    "toolbox-installation-binaries",
    "bin",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox application and uninstaller binaries",
)
_TOOLBOX_INTERNAL_TOOLS_RULE = _rule(
    "toolbox-internal-tools",
    "internal-tools",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox managed internal tools and clients",
)
_TOOLBOX_PORTS_RULE = _rule(
    "toolbox-ipc-port-state",
    r"cache\ports",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "JetBrains Toolbox IDE notification port state",
)
_TOOLBOX_PLUGIN_RULE = _rule(
    "toolbox-plugin-state",
    r"cache\plugins",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox plugin artifacts",
)
_TOOLBOX_ROOT_STATE_RULE = _rule(
    "toolbox-root-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "JetBrains Toolbox mixed application and persistent state",
)

TOOLBOX_RULES: tuple[ApplicationCleanupRule, ...] = (
    _TOOLBOX_DOWNLOAD_CACHE_RULE,
    _TOOLBOX_TEMP_RULE,
    _TOOLBOX_LOG_RULE,
    _TOOLBOX_SETTINGS_RULE,
    _TOOLBOX_ENVIRONMENT_RULE,
    _TOOLBOX_BIN_RULE,
    _TOOLBOX_INTERNAL_TOOLS_RULE,
    _TOOLBOX_PORTS_RULE,
    _TOOLBOX_PLUGIN_RULE,
    _TOOLBOX_ROOT_STATE_RULE,
)


def toolbox_root(
    environment: Mapping[str, str] | None = None,
) -> PureWindowsPath | None:
    env = _casefold_env(environment)
    explicit = env.get("devclean_toolbox_root")
    if explicit:
        candidate = PureWindowsPath(explicit)
        return candidate if candidate.is_absolute() else None
    localappdata = env.get("localappdata")
    if not localappdata:
        return None
    return PureWindowsPath(localappdata) / "JetBrains" / "Toolbox"


def toolbox_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    root = toolbox_root(environment)
    return () if root is None else (root,)


def match_toolbox_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    root = toolbox_root(environment)
    if root is None:
        return None
    normalized = _impl._normalize(path)
    normalized_root = _impl._normalize(root)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(TOOLBOX_RULES):
        relative = rule.relative_pattern
        candidate = normalized_root + ("\\" + relative if relative else "")
        if not _impl._matches(normalized, candidate, rule.match_kind):
            continue
        owner_weight = 3 if rule.owner is DecisionOwner.KEEP else 1
        matches.append((len(candidate), owner_weight * 1000 - index, rule))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def toolbox_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    root = toolbox_root(environment)
    if root is None:
        return ()
    return tuple(
        (root / rule.relative_pattern, rule)
        for rule in (
            _TOOLBOX_DOWNLOAD_CACHE_RULE,
            _TOOLBOX_TEMP_RULE,
            _TOOLBOX_LOG_RULE,
        )
    )


def whole_tree_toolbox_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in toolbox_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_toolbox_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_toolbox_rule(path, environment)
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

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = toolbox_process_running()
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
def toolbox_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'jetbrains-toolbox.exe' }; "
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


def clear_toolbox_process_cache() -> None:
    toolbox_process_running.cache_clear()


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "TOOLBOX_RULES",
    "clear_toolbox_process_cache",
    "evaluate_toolbox_path",
    "match_toolbox_rule",
    "toolbox_audited_tool_roots",
    "toolbox_process_running",
    "toolbox_root",
    "toolbox_scan_roots",
    "whole_tree_toolbox_rule",
]
