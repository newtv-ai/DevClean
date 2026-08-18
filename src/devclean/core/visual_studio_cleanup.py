r"""Audited Visual Studio IDE cache, servicing, and log semantics for Windows.

Exact source-backed regenerable roots receive TOOL ownership. Mixed WebTools and
per-user setup package state remain protected. Microsoft troubleshooting guidance
explicitly instructs deleting ``%TEMP%\servicehub\logs`` before reproducing an
out-of-process issue, which provides an exact supported cleanup boundary for
ServiceHub diagnostic logs.
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
_INSTANCE_SELECTOR = re.compile(r"^(?:16|17|18)\.0(?:_[0-9a-f]{4,})?$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VisualStudioRootSet:
    component_model_cache_roots: tuple[PureWindowsPath, ...]
    roslyn_cache_roots: tuple[PureWindowsPath, ...]
    web_tools_roots: tuple[PureWindowsPath, ...]
    local_package_roots: tuple[PureWindowsPath, ...]
    servicehub_log_roots: tuple[PureWindowsPath, ...]


_VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-component-model-cache",
    app_id="visual_studio",
    root_key="VISUAL_STUDIO_COMPONENT_MODEL_CACHE",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.DIRECTORY_MTIME,
    rebuild_cost=RebuildCost.MEDIUM,
    idle_days=30,
    min_reclaim_bytes=32 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
    label="Visual Studio MEF component model cache",
)
_VISUAL_STUDIO_ROSLYN_CACHE_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-roslyn-analyzer-cache",
    app_id="visual_studio",
    root_key="VISUAL_STUDIO_ROSLYN_CACHE",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.DIRECTORY_MTIME,
    rebuild_cost=RebuildCost.MEDIUM,
    idle_days=30,
    min_reclaim_bytes=128 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
    label="Visual Studio Roslyn analyzer cache",
)
_VISUAL_STUDIO_WEBTOOLS_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-webtools-mixed-state",
    app_id="visual_studio",
    root_key="VISUAL_STUDIO_WEBTOOLS",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.DIRECTORY_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Visual Studio WebTools mixed web/language-service state",
)
_VISUAL_STUDIO_LOCAL_PACKAGES_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-local-packages-servicing-state",
    app_id="visual_studio",
    root_key="VISUAL_STUDIO_LOCAL_PACKAGES",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.DIRECTORY_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Visual Studio per-user setup channel/package servicing state",
)
_VISUAL_STUDIO_SERVICEHUB_LOG_RULE = ApplicationCleanupRule(
    rule_id="visual-studio-servicehub-logs",
    app_id="visual_studio",
    root_key="VISUAL_STUDIO_SERVICEHUB_LOGS",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.DIRECTORY_MTIME,
    rebuild_cost=RebuildCost.NONE,
    idle_days=14,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
    label="Visual Studio ServiceHub diagnostic logs",
)

VISUAL_STUDIO_RULES: tuple[ApplicationCleanupRule, ...] = (
    _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE,
    _VISUAL_STUDIO_ROSLYN_CACHE_RULE,
    _VISUAL_STUDIO_WEBTOOLS_RULE,
    _VISUAL_STUDIO_LOCAL_PACKAGES_RULE,
    _VISUAL_STUDIO_SERVICEHUB_LOG_RULE,
)


def visual_studio_roots(
    environment: Mapping[str, str] | None = None,
) -> VisualStudioRootSet:
    env = _casefold_env(environment)
    local = env.get("localappdata")
    temp = env.get("temp") or env.get("tmp")

    component_caches: list[PureWindowsPath] = []
    web_tools: list[PureWindowsPath] = []
    roslyn_caches: tuple[PureWindowsPath, ...] = ()
    local_packages: tuple[PureWindowsPath, ...] = ()
    if local:
        parent = PureWindowsPath(local) / "Microsoft" / "VisualStudio"
        try:
            children = tuple(Path(str(parent)).iterdir())
        except OSError:
            children = ()
        for child in sorted(children, key=lambda item: item.name.casefold()):
            try:
                is_directory = child.is_dir()
            except OSError:
                continue
            if not is_directory or not _INSTANCE_SELECTOR.fullmatch(child.name):
                continue
            instance = PureWindowsPath(str(child))
            component_caches.append(instance / "ComponentModelCache")
            web_tools.append(instance / "WebTools")
        roslyn_caches = (parent / "Roslyn" / "Cache",)
        local_packages = (parent / "Packages",)

    servicehub_logs = (
        (PureWindowsPath(temp) / "servicehub" / "logs",) if temp else ()
    )
    return VisualStudioRootSet(
        tuple(component_caches),
        roslyn_caches,
        tuple(web_tools),
        local_packages,
        servicehub_logs,
    )


def visual_studio_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = visual_studio_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.component_model_cache_roots,
                *roots.roslyn_cache_roots,
                *roots.web_tools_roots,
                *roots.local_package_roots,
                *roots.servicehub_log_roots,
            )
        )
    )


def match_visual_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = visual_studio_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    for candidates, rule in (
        (roots.component_model_cache_roots, _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE),
        (roots.roslyn_cache_roots, _VISUAL_STUDIO_ROSLYN_CACHE_RULE),
        (roots.web_tools_roots, _VISUAL_STUDIO_WEBTOOLS_RULE),
        (roots.local_package_roots, _VISUAL_STUDIO_LOCAL_PACKAGES_RULE),
        (roots.servicehub_log_roots, _VISUAL_STUDIO_SERVICEHUB_LOG_RULE),
    ):
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
                matches.append((len(normalized_root), rule))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_visual_studio_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_visual_studio_rule(path, environment)
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

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None:
        running = visual_studio_process_running()
    score = _impl._benefit_score(logical_size, idle, threshold, rule.rebuild_cost)

    if running:
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


def visual_studio_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = visual_studio_roots(environment)
    return (
        *tuple(
            (root, _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE)
            for root in roots.component_model_cache_roots
        ),
        *tuple(
            (root, _VISUAL_STUDIO_ROSLYN_CACHE_RULE)
            for root in roots.roslyn_cache_roots
        ),
        *tuple(
            (root, _VISUAL_STUDIO_SERVICEHUB_LOG_RULE)
            for root in roots.servicehub_log_roots
        ),
    )


def whole_tree_visual_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = visual_studio_roots(environment)
    for candidates, rule in (
        (roots.component_model_cache_roots, _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE),
        (roots.roslyn_cache_roots, _VISUAL_STUDIO_ROSLYN_CACHE_RULE),
        (roots.servicehub_log_roots, _VISUAL_STUDIO_SERVICEHUB_LOG_RULE),
    ):
        for root in candidates:
            if normalized == _impl._normalize(root):
                return rule
    return None


@lru_cache(maxsize=1)
def visual_studio_process_running() -> bool:
    """Fail closed while Visual Studio or one of its ServiceHub satellites runs."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -eq 'devenv' -or $_.ProcessName -like 'ServiceHub*' }; "
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


def clear_visual_studio_process_cache() -> None:
    visual_studio_process_running.cache_clear()


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "VISUAL_STUDIO_RULES",
    "VisualStudioRootSet",
    "clear_visual_studio_process_cache",
    "evaluate_visual_studio_path",
    "match_visual_studio_rule",
    "visual_studio_audited_tool_roots",
    "visual_studio_process_running",
    "visual_studio_roots",
    "visual_studio_scan_roots",
    "whole_tree_visual_studio_rule",
]
