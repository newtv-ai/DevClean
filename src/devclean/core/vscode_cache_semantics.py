"""Keep audited VS Code generated caches safe independent of age/size heuristics.

The VS Code profile already distinguishes rebuildable caches from workspace
state, local history, persistent Service Worker storage, backups and installed
extensions. For those exact TOOL-owned generated cache classes, recency and
reclaim size are ranking inputs rather than a safety boundary.

Diagnostic logs, Crashpad reports and portable tmp are excluded here because
their retention semantics are not identical to generated caches.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import vscode_cleanup as _vscode
from devclean.core._application_cleanup_impl import (
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_VSCODE_PATH = _vscode.evaluate_vscode_path

_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "vscode-cache",
        "vscode-cached-data",
        "vscode-cached-configurations",
        "vscode-cached-profiles",
        "vscode-cached-extensions",
        "vscode-code-cache",
        "vscode-gpu-cache",
        "vscode-dawn-cache",
        "vscode-grshader-cache",
        "vscode-shader-cache",
        "vscode-service-worker-script-cache",
        "vscode-extension-vsix-cache",
        "vscode-wsl-server-download-cache",
    }
)


def evaluate_vscode_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Preserve benefit metadata while retaining source-backed cache safety."""

    decision = _ORIGINAL_EVALUATE_VSCODE_PATH(
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
    if getattr(_vscode, "_devclean_vscode_cache_semantics", False):
        return
    _vscode.evaluate_vscode_path = evaluate_vscode_path
    vars(_application)["evaluate_vscode_path"] = evaluate_vscode_path
    vars(_vscode)["_devclean_vscode_cache_semantics"] = True


install()


__all__ = ["evaluate_vscode_path", "install"]
