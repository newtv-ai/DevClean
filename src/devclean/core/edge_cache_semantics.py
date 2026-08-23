"""Keep audited Microsoft Edge generated caches safe independent of heuristics.

Edge reuses Chromium's source-backed browser cache boundaries while keeping
Microsoft updater state and diagnostic logs in separate protected rules. Age and
minimum reclaim size remain useful ranking inputs, but they must not turn those
known rebuildable browser caches into non-cleanup decisions. Edge/update process
activity remains the live execution guard.

Persistent site CacheStorage, browser profile state and Edge updater state/logs
are not included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import edge_cleanup as _edge
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_EDGE_PATH = _edge.evaluate_edge_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "edge-component-crx-cache",
        "edge-shader-cache",
        "edge-grshader-cache",
        "edge-graphite-dawn-cache",
        "edge-gpu-persistent-cache",
        "edge-font-lookup-cache",
        "edge-http-cache",
        "edge-media-cache",
        "edge-code-cache",
        "edge-profile-gpu-cache",
        "edge-service-worker-script-cache",
        "edge-explicit-disk-cache",
    }
)


def evaluate_edge_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Edge cache safety."""

    decision = _ORIGINAL_EVALUATE_EDGE_PATH(
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
    if getattr(_edge, "_devclean_edge_cache_semantics", False):
        return
    _edge.evaluate_edge_path = evaluate_edge_path
    vars(_application)["evaluate_edge_path"] = evaluate_edge_path
    vars(_edge)["_devclean_edge_cache_semantics"] = True


install()


__all__ = ["evaluate_edge_path", "install"]
