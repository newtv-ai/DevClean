"""Public semantic boundary for application-aware cleanup.

The stable Codex engine and each audited application profile live behind this
facade. Generic scan/delete code imports only this module, so USER-owned history
and KEEP state can be inventoried but never receive generic deletion authority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import PureWindowsPath

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
    effective_idle_days,
)
from devclean.core.claude_cleanup import (
    CLAUDE_RULES,
    claude_application_roots,
    claude_process_running,
    claude_scan_roots,
    clear_claude_process_cache,
    evaluate_claude_path,
    match_claude_rule,
)

_ORIGINAL_APPLICATION_ROOTS = _impl.application_roots
_ORIGINAL_EVALUATE_APPLICATION_PATH = _impl.evaluate_application_path
_ORIGINAL_MATCH_APPLICATION_RULE = _impl.match_application_rule
_ORIGINAL_APPLICATION_PROCESS_RUNNING = _impl.application_process_running


def application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    """Return all audited application roots, including redirected locations."""

    return (*_ORIGINAL_APPLICATION_ROOTS(environment), *claude_application_roots(environment))


def application_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return application-specific roots that may contain reclaimable storage."""

    codex = tuple(root.path for root in _ORIGINAL_APPLICATION_ROOTS(environment))
    return tuple(dict.fromkeys((*codex, *claude_scan_roots(environment))))


def match_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return the most-specific audited application rule for *path*."""

    claude = match_claude_rule(path, environment)
    if claude is not None:
        return claude
    return _ORIGINAL_MATCH_APPLICATION_RULE(path, environment)


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

    TOOL items retain their normal recommendation. USER-owned data keeps its
    metadata internally but is projected to ``KEEP_PROTECTED`` for the generic
    pipeline; a dedicated application action is the only place a user can choose
    to remove it.
    """

    decision = evaluate_claude_path(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    if decision is None:
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


def application_process_running(app_id: str) -> bool:
    if app_id == "claude":
        return claude_process_running()
    return _ORIGINAL_APPLICATION_PROCESS_RUNNING(app_id)


def clear_process_cache() -> None:
    _impl.clear_process_cache()
    clear_claude_process_cache()


def process_guard_allows(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Refuse USER/KEEP mutation, then re-check any application process guard."""

    rule = match_application_rule(path, environment)
    if rule is not None and rule.owner is not DecisionOwner.TOOL:
        return False
    if rule is None or not rule.requires_process_closed:
        return True
    clear_process_cache()
    return not application_process_running(rule.app_id)


def whole_tree_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return a TOOL rule only when *path* is exactly its audited whole-tree root."""

    rule = match_application_rule(path, environment)
    if (
        rule is None
        or rule.owner is not DecisionOwner.TOOL
        or not rule.allow_whole_tree
        or rule.root_key == "ANYWHERE"
    ):
        return None
    roots = {root.key: root.path for root in application_roots(environment)}
    root = roots.get(rule.root_key)
    if root is None:
        return None
    normalized = _impl._normalize(path)
    for expanded in _impl._expand_braces(rule.relative_pattern):
        candidate = PureWindowsPath(root) / expanded if expanded else PureWindowsPath(root)
        if normalized == _impl._normalize(candidate):
            return rule
    return None


def application_display_name(app_id: str) -> str:
    return {"codex": "Codex", "claude": "Claude Code"}.get(app_id, app_id)


__all__ = [
    "CLAUDE_RULES",
    "CODEX_RULES",
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
    "DecisionOwner",
    "LastUseStrategy",
    "MatchKind",
    "PolicyAction",
    "RebuildCost",
    "application_display_name",
    "application_process_running",
    "application_roots",
    "application_scan_roots",
    "clear_process_cache",
    "effective_idle_days",
    "evaluate_application_path",
    "match_application_rule",
    "process_guard_allows",
    "whole_tree_application_rule",
]
