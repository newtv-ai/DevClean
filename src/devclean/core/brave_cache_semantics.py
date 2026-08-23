"""Keep audited Brave generated caches safe independent of heuristics.

Brave clones Chromium's source-backed browser cache boundaries while keeping its
Omaha updater working tree, logs and updater state in separate protected rules.
Age and minimum reclaim size remain ranking inputs, but they must not revoke the
safe-clean classification of those exact rebuildable browser caches. Brave/update
process activity remains the live execution guard.

Persistent site CacheStorage, browser profile state and Brave updater state/logs
are not included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import brave_cleanup as _brave
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_BRAVE_PATH = _brave.evaluate_brave_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "brave-component-crx-cache",
        "brave-shader-cache",
        "brave-grshader-cache",
        "brave-graphite-dawn-cache",
        "brave-gpu-persistent-cache",
        "brave-font-lookup-cache",
        "brave-http-cache",
        "brave-media-cache",
        "brave-code-cache",
        "brave-profile-gpu-cache",
        "brave-service-worker-script-cache",
        "brave-explicit-disk-cache",
    }
)


def evaluate_brave_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Brave cache safety."""

    decision = _ORIGINAL_EVALUATE_BRAVE_PATH(
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
    if getattr(_brave, "_devclean_brave_cache_semantics", False):
        return
    _brave.evaluate_brave_path = evaluate_brave_path
    vars(_application)["evaluate_brave_path"] = evaluate_brave_path
    vars(_brave)["_devclean_brave_cache_semantics"] = True


install()


__all__ = ["evaluate_brave_path", "install"]
