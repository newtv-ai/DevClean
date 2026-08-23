"""Keep audited Vivaldi generated caches safe independent of heuristics.

Vivaldi clones Chromium's source-backed browser cache boundaries, including its
standalone/user-data discovery, while Crashpad reports and profile state remain
separately protected. Age and minimum reclaim size remain ranking inputs but do
not revoke the safe-clean classification of exact rebuildable browser caches.
Vivaldi process activity remains the live execution guard.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import vivaldi_cleanup as _vivaldi
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_VIVALDI_PATH = _vivaldi.evaluate_vivaldi_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "vivaldi-component-crx-cache",
        "vivaldi-shader-cache",
        "vivaldi-grshader-cache",
        "vivaldi-graphite-dawn-cache",
        "vivaldi-gpu-persistent-cache",
        "vivaldi-font-lookup-cache",
        "vivaldi-http-cache",
        "vivaldi-media-cache",
        "vivaldi-code-cache",
        "vivaldi-profile-gpu-cache",
        "vivaldi-service-worker-script-cache",
        "vivaldi-explicit-disk-cache",
    }
)


def evaluate_vivaldi_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Vivaldi cache safety."""

    decision = _ORIGINAL_EVALUATE_VIVALDI_PATH(
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
    if getattr(_vivaldi, "_devclean_vivaldi_cache_semantics", False):
        return
    _vivaldi.evaluate_vivaldi_path = evaluate_vivaldi_path
    vars(_application)["evaluate_vivaldi_path"] = evaluate_vivaldi_path
    vars(_vivaldi)["_devclean_vivaldi_cache_semantics"] = True


install()


__all__ = ["evaluate_vivaldi_path", "install"]
