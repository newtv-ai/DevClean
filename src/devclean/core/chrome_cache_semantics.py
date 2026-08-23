"""Keep audited Chrome/Chromium cache classes safe independent of heuristics.

The Chrome profile already grants TOOL ownership only to exact regenerable cache
boundaries proven by Chromium/Updater semantics. Age and minimum reclaim size
remain useful ranking inputs, but they must not make a known cache stop being a
safe-clean candidate. Browser/updater activity remains the live execution guard.

Persistent site CacheStorage, browser profile state, cookies/history/logins,
updater state and legacy Omaha state are not included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import chrome_cleanup as _chrome
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_CHROME_PATH = _chrome.evaluate_chrome_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "chrome-component-crx-cache",
        "chrome-shader-cache",
        "chrome-grshader-cache",
        "chrome-graphite-dawn-cache",
        "chrome-gpu-persistent-cache",
        "chrome-font-lookup-cache",
        "chrome-http-cache",
        "chrome-media-cache",
        "chrome-code-cache",
        "chrome-profile-gpu-cache",
        "chrome-service-worker-script-cache",
        "chrome-explicit-disk-cache",
        "chrome-updater-crx-cache",
    }
)


def evaluate_chrome_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Chrome cache safety."""

    decision = _ORIGINAL_EVALUATE_CHROME_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    if decision is None:
        return None
    if decision.rule.owner is not DecisionOwner.TOOL:
        return decision
    if decision.rule.rule_id not in _SAFE_CACHE_RULE_IDS:
        return decision
    if decision.action is PolicyAction.TOOL_KEEP_IN_USE:
        return decision
    return replace(decision, action=PolicyAction.TOOL_DELETE)


def install() -> None:
    if getattr(_chrome, "_devclean_chrome_cache_semantics", False):
        return
    _chrome.evaluate_chrome_path = evaluate_chrome_path
    vars(_application)["evaluate_chrome_path"] = evaluate_chrome_path
    vars(_chrome)["_devclean_chrome_cache_semantics"] = True


install()


__all__ = ["evaluate_chrome_path", "install"]
