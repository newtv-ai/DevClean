"""Keep audited Firefox cache roots safe independent of heuristics.

Mozilla distinguishes persistent profile state (ProfD) from the profile-local
cache directory (ProfLD), and DevClean already grants TOOL ownership only to the
exact cache boundaries established by that audit. Age and minimum reclaim size
remain ranking inputs but do not revoke cleanup safety for those boundaries.
Firefox/update process activity remains the live execution guard.

Persistent profile state, crash reports and updater state/logs are not included.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import firefox_cleanup as _firefox
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_FIREFOX_PATH = _firefox.evaluate_firefox_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "firefox-local-profile-cache-root",
        "firefox-cache2",
        "firefox-startup-cache",
        "firefox-jumplist-cache",
    }
)


def evaluate_firefox_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata without revoking audited Firefox cache safety."""

    decision = _ORIGINAL_EVALUATE_FIREFOX_PATH(
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
    if getattr(_firefox, "_devclean_firefox_cache_semantics", False):
        return
    _firefox.evaluate_firefox_path = evaluate_firefox_path
    vars(_application)["evaluate_firefox_path"] = evaluate_firefox_path
    vars(_firefox)["_devclean_firefox_cache_semantics"] = True


install()


__all__ = ["evaluate_firefox_path", "install"]
