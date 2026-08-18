r"""Audited Visual Studio IDE component-cache semantics for Windows cleanup.

Visual Studio keeps per-instance MEF composition state under ComponentModelCache.
Microsoft troubleshooting guidance explicitly rebuilds that cache by removing it
while Visual Studio is closed. DevClean delegates only that exact subtree; the
surrounding per-instance state remains outside generic deletion authority.
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

VISUAL_STUDIO_RULES: tuple[ApplicationCleanupRule, ...] = (
    _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE,
)


def visual_studio_roots(
    environment: Mapping[str, str] | None = None,
) -> VisualStudioRootSet:
    env = _casefold_env(environment)
    local = env.get("localappdata")
    if not local:
        return VisualStudioRootSet(())

    parent = PureWindowsPath(local) / "Microsoft" / "VisualStudio"
    caches: list[PureWindowsPath] = []
    try:
        children = tuple(Path(str(parent)).iterdir())
    except OSError:
        children = ()
    for child in children:
        try:
            is_directory = child.is_dir()
        except OSError:
            continue
        if not is_directory or not _INSTANCE_SELECTOR.fullmatch(child.name):
            continue
        caches.append(PureWindowsPath(str(child)) / "ComponentModelCache")
    return VisualStudioRootSet(tuple(caches))


def visual_studio_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return visual_studio_roots(environment).component_model_cache_roots


def match_visual_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    matches: list[int] = []
    for root in visual_studio_roots(environment).component_model_cache_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            matches.append(len(normalized_root))
    if not matches:
        return None
    return _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE


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
    return tuple(
        (root, _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE)
        for root in visual_studio_roots(environment).component_model_cache_roots
    )


def whole_tree_visual_studio_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    for root in visual_studio_roots(environment).component_model_cache_roots:
        if normalized == _impl._normalize(root):
            return _VISUAL_STUDIO_COMPONENT_MODEL_CACHE_RULE
    return None


@lru_cache(maxsize=1)
def visual_studio_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -Name devenv -ErrorAction SilentlyContinue; "
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
