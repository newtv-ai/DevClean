r"""Audited Puppeteer browser-cache storage semantics for Windows cleanup.

Puppeteer downloads browser runtimes into a shared cache by default. DevClean
inventories that exact cache root but does not infer version liveness or raw-delete
browser builds that may still be required by another Puppeteer installation.
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
class PuppeteerRootSet:
    browser_cache_roots: tuple[PureWindowsPath, ...]
    relative_cache_override: bool


_PUPPETEER_BROWSER_CACHE_RULE = ApplicationCleanupRule(
    rule_id="puppeteer-browser-cache-vendor-managed",
    app_id="puppeteer",
    root_key="PUPPETEER_CACHE_DIR",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Puppeteer downloaded browser cache; version liveness is owned by Puppeteer",
)

PUPPETEER_RULES: tuple[ApplicationCleanupRule, ...] = (
    _PUPPETEER_BROWSER_CACHE_RULE,
)


def puppeteer_roots(
    environment: Mapping[str, str] | None = None,
) -> PuppeteerRootSet:
    env = _casefold_env(environment)
    configured = env.get("puppeteer_cache_dir")
    relative_override = False

    cache: PureWindowsPath | None = None
    if configured:
        candidate = PureWindowsPath(configured)
        if candidate.is_absolute():
            cache = candidate
        else:
            relative_override = True
    else:
        profile = env.get("userprofile")
        if profile:
            cache = PureWindowsPath(profile) / ".cache" / "puppeteer"

    return PuppeteerRootSet(
        browser_cache_roots=() if cache is None else (cache,),
        relative_cache_override=relative_override,
    )


def puppeteer_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return puppeteer_roots(environment).browser_cache_roots


def match_puppeteer_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    for root in puppeteer_roots(environment).browser_cache_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            return _PUPPETEER_BROWSER_CACHE_RULE
    return None


def evaluate_puppeteer_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_puppeteer_rule(path, environment)
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


def puppeteer_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_puppeteer_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def puppeteer_process_running() -> bool:
    """No mutation path is exposed, so no process gate is required yet."""

    return False


def clear_puppeteer_process_cache() -> None:
    return None


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "PUPPETEER_RULES",
    "PuppeteerRootSet",
    "clear_puppeteer_process_cache",
    "evaluate_puppeteer_path",
    "match_puppeteer_rule",
    "puppeteer_audited_tool_roots",
    "puppeteer_process_running",
    "puppeteer_roots",
    "puppeteer_scan_roots",
    "whole_tree_puppeteer_rule",
]
