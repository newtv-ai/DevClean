"""Public semantic boundary for application-aware cleanup.

The stable rule/evaluation engine lives in ``_application_cleanup_impl``.
Generic scan/delete code imports this module, which adds one non-negotiable
boundary: USER-owned history and KEEP state may be inventoried, but they never
receive generic exact-file deletion authority. USER data is handled only by a
dedicated application action such as the Codex history manager.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    CODEX_RULES,
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    ApplicationRoot,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    application_process_running,
    application_roots,
    clear_process_cache,
    effective_idle_days,
    match_application_rule,
)

_ORIGINAL_EVALUATE_APPLICATION_PATH = _impl.evaluate_application_path
_ORIGINAL_PROCESS_GUARD_ALLOWS = _impl.process_guard_allows


def evaluate_application_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Evaluate one path for the generic scan/review pipeline.

    TOOL items retain the engine's normal recommendation. USER-owned data keeps
    its owner, last-use timestamp, age bucket and benefit estimate, but the
    generic action is projected to ``KEEP_PROTECTED`` so it cannot enter AI or
    the exact-file deletion lane. Dedicated application tools consume USER data
    separately and require an explicit user choice.
    """

    decision = _ORIGINAL_EVALUATE_APPLICATION_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    if decision is None or decision.rule.owner is not DecisionOwner.USER:
        return decision
    return replace(decision, action=PolicyAction.KEEP_PROTECTED)


def process_guard_allows(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Refuse generic mutation for USER/KEEP paths, then apply process guards."""

    rule = match_application_rule(path, environment)
    if rule is not None and rule.owner is not DecisionOwner.TOOL:
        return False
    return _ORIGINAL_PROCESS_GUARD_ALLOWS(path, environment)


__all__ = [
    "CODEX_RULES",
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
    "DecisionOwner",
    "LastUseStrategy",
    "MatchKind",
    "PolicyAction",
    "RebuildCost",
    "application_process_running",
    "application_roots",
    "clear_process_cache",
    "effective_idle_days",
    "evaluate_application_path",
    "match_application_rule",
    "process_guard_allows",
]
