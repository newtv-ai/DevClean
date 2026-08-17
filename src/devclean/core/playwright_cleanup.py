r"""Audited Playwright browser-binary storage semantics for Windows cleanup.

Playwright owns lifecycle/garbage collection for its downloaded browser builds.
DevClean inventories the exact shared browser registry but does not raw-delete
binaries that may still be required by installed Playwright clients.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
)


@dataclass(frozen=True, slots=True)
class PlaywrightRootSet:
    browser_registry_roots: tuple[PureWindowsPath, ...]
    project_local_browsers: bool
    browser_gc_disabled: bool


_PLAYWRIGHT_BROWSER_REGISTRY_RULE = ApplicationCleanupRule(
    rule_id="playwright-browser-registry-vendor-managed",
    app_id="playwright",
    root_key="PLAYWRIGHT_BROWSERS",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Playwright downloaded browser registry; lifecycle is managed by Playwright",
)

PLAYWRIGHT_RULES: tuple[ApplicationCleanupRule, ...] = (
    _PLAYWRIGHT_BROWSER_REGISTRY_RULE,
)


def playwright_roots(
    environment: Mapping[str, str] | None = None,
) -> PlaywrightRootSet:
    env = _casefold_env(environment)
    configured = env.get("playwright_browsers_path")
    project_local = configured == "0"

    registry: PureWindowsPath | None = None
    if configured and configured != "0":
        candidate = PureWindowsPath(configured)
        if candidate.is_absolute():
            registry = candidate
    elif not project_local:
        local = env.get("localappdata")
        if local:
            registry = PureWindowsPath(local) / "ms-playwright"
        else:
            profile = env.get("userprofile")
            if profile:
                registry = (
                    PureWindowsPath(profile)
                    / "AppData"
                    / "Local"
                    / "ms-playwright"
                )

    return PlaywrightRootSet(
        browser_registry_roots=() if registry is None else (registry,),
        project_local_browsers=project_local,
        browser_gc_disabled=env.get("playwright_skip_browser_gc") == "1",
    )


def playwright_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return playwright_roots(environment).browser_registry_roots


def match_playwright_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    for root in playwright_roots(environment).browser_registry_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            return _PLAYWRIGHT_BROWSER_REGISTRY_RULE
    return None


def evaluate_playwright_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_playwright_rule(path, environment)
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
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def playwright_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_playwright_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def playwright_process_running() -> bool:
    """No mutation path is exposed, so no process gate is required yet."""

    return False


def clear_playwright_process_cache() -> None:
    return None


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "PLAYWRIGHT_RULES",
    "PlaywrightRootSet",
    "clear_playwright_process_cache",
    "evaluate_playwright_path",
    "match_playwright_rule",
    "playwright_audited_tool_roots",
    "playwright_process_running",
    "playwright_roots",
    "playwright_scan_roots",
    "whole_tree_playwright_rule",
]
