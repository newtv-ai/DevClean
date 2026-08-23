"""Second-pass Trae and Windsurf cache/diagnostic semantics.

Exact source-audited Electron/Code-OSS generated caches remain safe cleanup
objects when the owning editor is closed.  Age, size and last-use metadata rank
benefit; they do not revoke that safety.

Diagnostic artifacts have a different product meaning.  Trae support explicitly
allows users to remove unneeded logs, while Windsurf documents its logs as
support/troubleshooting material.  Those plain-log roots are therefore USER
review items: technically understood, but retention depends on whether the user
still needs diagnostics.  Crashpad reports/pending state remain KEEP because
Crashpad owns report lifecycle through its report database rather than a raw
filesystem-age contract.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from devclean.core import application_cleanup as _application
from devclean.core import trae_cleanup as _trae
from devclean.core import windsurf_cleanup as _windsurf
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    PolicyAction,
)

_ORIGINAL_EVALUATE_TRAE_PATH = _trae.evaluate_trae_path
_ORIGINAL_EVALUATE_WINDSURF_PATH = _windsurf.evaluate_windsurf_path

_TRAE_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "trae-cache",
        "trae-cached-data",
        "trae-code-cache",
        "trae-gpu-cache",
        "trae-dawn-cache",
        "trae-cached-extensions",
        "trae-cached-extension-vsix",
    }
)

_WINDSURF_SAFE_CACHE_RULE_IDS = frozenset(
    {
        "windsurf-cache",
        "windsurf-cached-data",
        "windsurf-cached-configurations",
        "windsurf-cached-profiles",
        "windsurf-cached-extensions",
        "windsurf-code-cache",
        "windsurf-gpu-cache",
        "windsurf-dawn-cache",
        "windsurf-grshader-cache",
        "windsurf-shader-cache",
        "windsurf-service-worker-script-cache",
        "windsurf-extension-vsix-cache",
    }
)


def _user_log_rule(
    rule: ApplicationCleanupRule,
    *,
    label: str,
) -> ApplicationCleanupRule:
    return replace(
        rule,
        owner=DecisionOwner.USER,
        idle_days=None,
        min_reclaim_bytes=0,
        requires_process_closed=True,
        user_age_buckets=(7, 30, 90),
        allow_whole_tree=False,
        label=label,
    )


def _protected_crashpad_rule(
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
        user_age_buckets=(),
        allow_whole_tree=False,
        label=label,
    )


def _replace_rules(
    rules: tuple[ApplicationCleanupRule, ...],
    replacements: Mapping[str, ApplicationCleanupRule],
) -> tuple[ApplicationCleanupRule, ...]:
    return tuple(replacements.get(rule.rule_id, rule) for rule in rules)


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


def evaluate_trae_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    decision = _ORIGINAL_EVALUATE_TRAE_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    return _force_safe_cache_action(decision, _TRAE_SAFE_CACHE_RULE_IDS)


def evaluate_windsurf_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    decision = _ORIGINAL_EVALUATE_WINDSURF_PATH(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
        process_running=process_running,
        environment=environment,
    )
    return _force_safe_cache_action(decision, _WINDSURF_SAFE_CACHE_RULE_IDS)


def install() -> None:
    if getattr(_trae, "_devclean_trae_windsurf_cache_semantics", False):
        return

    trae_by_id = {rule.rule_id: rule for rule in _trae.TRAE_RULES}
    windsurf_by_id = {rule.rule_id: rule for rule in _windsurf.WINDSURF_RULES}

    trae_replacements = {
        "trae-logs": _user_log_rule(
            trae_by_id["trae-logs"],
            label=(
                "Trae diagnostic logs; user decides whether troubleshooting "
                "history is still needed"
            ),
        ),
        "trae-crashpad-reports": _protected_crashpad_rule(
            trae_by_id["trae-crashpad-reports"],
            label="Trae Crashpad completed diagnostic reports managed by Crashpad",
        ),
        "trae-crashpad-pending": _protected_crashpad_rule(
            trae_by_id["trae-crashpad-pending"],
            label="Trae pending Crashpad diagnostic reports managed by Crashpad",
        ),
    }
    windsurf_replacements = {
        "windsurf-logs": _user_log_rule(
            windsurf_by_id["windsurf-logs"],
            label="Windsurf diagnostic logs retained at the user's discretion for support",
        ),
        "windsurf-crashpad-reports": _protected_crashpad_rule(
            windsurf_by_id["windsurf-crashpad-reports"],
            label="Windsurf Crashpad completed diagnostic reports managed by Crashpad",
        ),
        "windsurf-crashpad-pending": _protected_crashpad_rule(
            windsurf_by_id["windsurf-crashpad-pending"],
            label="Windsurf pending Crashpad diagnostic reports managed by Crashpad",
        ),
    }

    _trae.TRAE_RULES = _replace_rules(_trae.TRAE_RULES, trae_replacements)
    _windsurf.WINDSURF_RULES = _replace_rules(
        _windsurf.WINDSURF_RULES,
        windsurf_replacements,
    )
    _trae.evaluate_trae_path = evaluate_trae_path
    _windsurf.evaluate_windsurf_path = evaluate_windsurf_path

    vars(_application)["TRAE_RULES"] = _trae.TRAE_RULES
    vars(_application)["WINDSURF_RULES"] = _windsurf.WINDSURF_RULES
    vars(_application)["evaluate_trae_path"] = evaluate_trae_path
    vars(_application)["evaluate_windsurf_path"] = evaluate_windsurf_path

    vars(_trae)["_devclean_trae_windsurf_cache_semantics"] = True


install()


__all__ = [
    "evaluate_trae_path",
    "evaluate_windsurf_path",
    "install",
]
