r"""Audited Cypress binary-cache semantics for Windows cleanup.

Cypress keeps its large application binaries in a global cache shared by projects.
DevClean inventories the exact effective cache but does not raw-delete versions
that may still be required by another project using a different Cypress release.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
)


@dataclass(frozen=True, slots=True)
class CypressRootSet:
    binary_cache_roots: tuple[PureWindowsPath, ...]
    app_data_roots: tuple[PureWindowsPath, ...]
    run_binary_paths: tuple[PureWindowsPath, ...]
    relative_cache_override: bool


_CYPRESS_BINARY_CACHE_RULE = ApplicationCleanupRule(
    rule_id="cypress-binary-cache-vendor-managed",
    app_id="cypress",
    root_key="CYPRESS_CACHE_FOLDER",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Cypress shared binary cache; version liveness is owned by Cypress projects",
)
_CYPRESS_APP_DATA_RULE = ApplicationCleanupRule(
    rule_id="cypress-app-data-state",
    app_id="cypress",
    root_key="CYPRESS_APP_DATA",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Cypress application data and persistent state",
)
_CYPRESS_RUN_BINARY_RULE = ApplicationCleanupRule(
    rule_id="cypress-external-run-binary",
    app_id="cypress",
    root_key="CYPRESS_RUN_BINARY",
    relative_pattern="",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="User-selected Cypress runtime binary",
)

CYPRESS_RULES: tuple[ApplicationCleanupRule, ...] = (
    _CYPRESS_BINARY_CACHE_RULE,
    _CYPRESS_APP_DATA_RULE,
    _CYPRESS_RUN_BINARY_RULE,
)


def cypress_roots(
    environment: Mapping[str, str] | None = None,
) -> CypressRootSet:
    env = _casefold_env(environment)
    profile = env.get("userprofile")
    local = env.get("localappdata")
    roaming = env.get("appdata")

    configured = _first_value(
        env,
        "cypress_cache_folder",
        "npm_config_cypress_cache_folder",
        "npm_package_config_cypress_cache_folder",
    )
    relative_override = False
    cache: PureWindowsPath | None = None
    if configured:
        expanded = _expand_home(configured, profile)
        candidate = PureWindowsPath(expanded)
        if candidate.is_absolute():
            cache = candidate
        else:
            # Cypress resolves relative cache folders against its invocation cwd.
            # DevClean has no authoritative project/cwd here, so do not guess.
            relative_override = True
    elif local:
        cache = PureWindowsPath(local) / "Cypress" / "Cache"
    elif profile:
        cache = PureWindowsPath(profile) / "AppData" / "Local" / "Cypress" / "Cache"

    app_data: PureWindowsPath | None = None
    if roaming:
        app_data = PureWindowsPath(roaming) / "Cypress"
    elif profile:
        app_data = PureWindowsPath(profile) / "AppData" / "Roaming" / "Cypress"

    run_binary = _absolute_path(
        _first_value(
            env,
            "cypress_run_binary",
            "npm_config_cypress_run_binary",
            "npm_package_config_cypress_run_binary",
        ),
        profile,
    )

    return CypressRootSet(
        binary_cache_roots=_tuple_if_path(cache),
        app_data_roots=_tuple_if_path(app_data),
        run_binary_paths=_tuple_if_path(run_binary),
        relative_cache_override=relative_override,
    )


def cypress_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return only the shared binary cache as a storage inventory anchor."""

    return cypress_roots(environment).binary_cache_roots


def match_cypress_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = cypress_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for candidates, rule, kind in (
        (roots.binary_cache_roots, _CYPRESS_BINARY_CACHE_RULE, MatchKind.PREFIX),
        (roots.app_data_roots, _CYPRESS_APP_DATA_RULE, MatchKind.PREFIX),
        (roots.run_binary_paths, _CYPRESS_RUN_BINARY_RULE, MatchKind.EXACT),
    ):
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, kind):
                # Exact user-selected runtime paths outrank enclosing directories.
                kind_weight = 1 if kind is MatchKind.EXACT else 0
                matches.append((len(normalized_root), kind_weight, rule))

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def evaluate_cypress_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_cypress_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = (
        None
        if observed is None
        else max(0.0, (current - observed).total_seconds() / 86_400)
    )
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def cypress_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_cypress_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def cypress_process_running() -> bool:
    """No mutation path is exposed, so no process gate is required yet."""

    return False


def clear_cypress_process_cache() -> None:
    return None


def _first_value(env: Mapping[str, str], *keys: str) -> str | None:
    for key in keys:
        value = env.get(key)
        if value:
            return value
    return None


def _expand_home(value: str, profile: str | None) -> str:
    if value == "~":
        return profile or value
    if value.startswith(("~/", "~\\")) and profile:
        return str(PureWindowsPath(profile) / value[2:])
    return value


def _absolute_path(value: str | None, profile: str | None) -> PureWindowsPath | None:
    if not value:
        return None
    candidate = PureWindowsPath(_expand_home(value, profile))
    return candidate if candidate.is_absolute() else None


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "CYPRESS_RULES",
    "CypressRootSet",
    "clear_cypress_process_cache",
    "cypress_audited_tool_roots",
    "cypress_process_running",
    "cypress_roots",
    "cypress_scan_roots",
    "evaluate_cypress_path",
    "match_cypress_rule",
    "whole_tree_cypress_rule",
]
