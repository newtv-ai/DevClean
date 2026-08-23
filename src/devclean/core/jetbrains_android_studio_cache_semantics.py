"""Second-pass JetBrains / Android Studio cache semantics.

The source-audited IntelliJ-platform ``index``, ``tmp`` and ``vcs-log``
subtrees are exact rebuildable TOOL roots.  Their age/size metadata is useful
for ranking, but it must not revoke source-backed cleanup safety once the owning
IDE is closed.

Current product logs are different.  JetBrains documents the log directory as
product logs/thread dumps, and its automatic 180-day cleanup applies to old IDE
data-directory groups rather than granting generic raw deletion authority over
the active product log root.  Android Studio likewise documents the log path
without a raw age-based deletion lifecycle.  Keep current logs protected here;
old JetBrains-version maintenance remains a separate, narrower vendor-lifecycle
lane.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import android_studio_cleanup as _android
from devclean.core import application_cleanup as _application
from devclean.core import jetbrains_cleanup as _jetbrains
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_JETBRAINS_PATH = _jetbrains.evaluate_jetbrains_path
_ORIGINAL_EVALUATE_ANDROID_STUDIO_PATH = _android.evaluate_android_studio_path

_JETBRAINS_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "jetbrains-index-cache",
        "jetbrains-system-temp",
        "jetbrains-vcs-log-cache",
    }
)
_ANDROID_STUDIO_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "android-studio-index-cache",
        "android-studio-system-temp",
        "android-studio-vcs-log-cache",
    }
)


def _protected_log_rule(
    rule: ApplicationCleanupRule,
    *,
    label: str,
) -> ApplicationCleanupRule:
    return replace(
        rule,
        owner=DecisionOwner.KEEP,
        idle_days=None,
        min_reclaim_bytes=0,
        requires_process_closed=False,
        allow_whole_tree=False,
        label=label,
    )


def _replace_rule(
    rules: tuple[ApplicationCleanupRule, ...],
    replacement: ApplicationCleanupRule,
) -> tuple[ApplicationCleanupRule, ...]:
    return tuple(
        replacement if rule.rule_id == replacement.rule_id else rule for rule in rules
    )


def _force_safe_cache_action(
    decision: ApplicationPolicyDecision | None,
    safe_rule_ids: frozenset[str],
) -> ApplicationPolicyDecision | None:
    if decision is None:
        return None
    if decision.rule.owner is not DecisionOwner.TOOL:
        return decision
    if decision.rule.rule_id not in safe_rule_ids:
        return decision
    if decision.action is PolicyAction.TOOL_KEEP_IN_USE:
        return decision
    return replace(decision, action=PolicyAction.TOOL_DELETE)


def evaluate_jetbrains_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    decision = _ORIGINAL_EVALUATE_JETBRAINS_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    return _force_safe_cache_action(decision, _JETBRAINS_SAFE_CACHE_RULE_IDS)


def evaluate_android_studio_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    decision = _ORIGINAL_EVALUATE_ANDROID_STUDIO_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    return _force_safe_cache_action(decision, _ANDROID_STUDIO_SAFE_CACHE_RULE_IDS)


def install() -> None:
    if getattr(_jetbrains, "_devclean_jetbrains_android_cache_semantics", False):
        return

    jetbrains_log = _protected_log_rule(
        _jetbrains._JETBRAINS_LOG_RULE,
        label="JetBrains current IDE diagnostic logs and thread dumps",
    )
    android_log = _protected_log_rule(
        _android._ANDROID_STUDIO_LOG_RULE,
        label="Android Studio current diagnostic logs and thread dumps",
    )

    _jetbrains._JETBRAINS_LOG_RULE = jetbrains_log
    _jetbrains.JETBRAINS_RULES = _replace_rule(_jetbrains.JETBRAINS_RULES, jetbrains_log)
    _android._ANDROID_STUDIO_LOG_RULE = android_log
    _android.ANDROID_STUDIO_RULES = _replace_rule(
        _android.ANDROID_STUDIO_RULES,
        android_log,
    )

    _jetbrains.evaluate_jetbrains_path = evaluate_jetbrains_path
    _android.evaluate_android_studio_path = evaluate_android_studio_path

    vars(_application)["JETBRAINS_RULES"] = _jetbrains.JETBRAINS_RULES
    vars(_application)["ANDROID_STUDIO_RULES"] = _android.ANDROID_STUDIO_RULES
    vars(_application)["evaluate_jetbrains_path"] = evaluate_jetbrains_path
    vars(_application)["evaluate_android_studio_path"] = evaluate_android_studio_path

    vars(_jetbrains)["_devclean_jetbrains_android_cache_semantics"] = True


install()


__all__ = [
    "evaluate_android_studio_path",
    "evaluate_jetbrains_path",
    "install",
]
