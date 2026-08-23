"""Keep audited Opera / Opera GX generated caches safe independent of heuristics.

Opera splits roaming profile state from local cache storage and supports both
legacy direct-child and newer Chromium Default/Profile-N layouts. Only cache
classes already delegated to TOOL ownership are covered here. Age and minimum
reclaim size remain ranking inputs but do not revoke their source-backed cleanup
safety. Opera/autoupdate process activity remains the live execution guard.

System Cache, recovery profile copies and mixed edition/profile state are not
included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import opera_cleanup as _opera
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_OPERA_PATH = _opera.evaluate_opera_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "opera-component-crx-cache",
        "opera-shader-cache",
        "opera-grshader-cache",
        "opera-graphite-dawn-cache",
        "opera-gpu-persistent-cache",
        "opera-font-lookup-cache",
        "opera-http-cache",
        "opera-media-cache",
        "opera-code-cache",
        "opera-profile-gpu-cache",
        "opera-service-worker-script-cache",
        "opera-explicit-disk-cache",
    }
)


def evaluate_opera_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Opera cache safety."""

    decision = _ORIGINAL_EVALUATE_OPERA_PATH(
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
    if getattr(_opera, "_devclean_opera_cache_semantics", False):
        return
    _opera.evaluate_opera_path = evaluate_opera_path
    vars(_application)["evaluate_opera_path"] = evaluate_opera_path
    vars(_opera)["_devclean_opera_cache_semantics"] = True


install()


__all__ = ["evaluate_opera_path", "install"]
