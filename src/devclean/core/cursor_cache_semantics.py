"""Make audited Cursor rebuildable caches safe independently of age/size heuristics.

Cursor's generated Chromium/editor caches are source-identified cache classes.
Once a path matches one of those exact rules, recency and reclaim size describe
benefit, not whether the bytes are user data. The only execution-time blocker is
Cursor/updater activity, which the existing process guard still re-checks.

Diagnostic logs and Crashpad reports are intentionally excluded here because
recent diagnostics have different retention semantics and will be audited as a
separate class.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import cursor_cleanup as _cursor
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_CURSOR_PATH = _cursor.evaluate_cursor_path

_CACHE_RULE_SUFFIXES = frozenset(
    {
        "-cache",
        "-cached-data",
        "-code-cache",
        "-gpu-cache",
        "-dawn-cache",
        "-grshader-cache",
        "-shader-cache",
        "-cached-extensions",
        "-cached-extension-vsix",
    }
)


def _is_disposable_cursor_cache(rule_id: str) -> bool:
    if not rule_id.startswith("cursor-"):
        return False
    return any(rule_id.endswith(suffix) for suffix in _CACHE_RULE_SUFFIXES)


def evaluate_cursor_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Keep ranking evidence, but never let age/size revoke audited cache safety."""

    decision = _ORIGINAL_EVALUATE_CURSOR_PATH(
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
    if not _is_disposable_cursor_cache(decision.rule.rule_id):
        return decision
    if decision.action is PolicyAction.TOOL_KEEP_IN_USE:
        return decision
    return replace(decision, action=PolicyAction.TOOL_DELETE)


def install() -> None:
    if getattr(_cursor, "_devclean_cursor_cache_semantics", False):
        return
    _cursor.evaluate_cursor_path = evaluate_cursor_path
    # application_cleanup imported the original callable into its module namespace,
    # so replace that snapshot as well.
    vars(_application)["evaluate_cursor_path"] = evaluate_cursor_path
    vars(_cursor)["_devclean_cursor_cache_semantics"] = True


install()


__all__ = ["evaluate_cursor_path", "install"]
