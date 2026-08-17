r"""Audited Electron download-cache semantics for Windows cleanup.

Electron's installer uses @electron/get to cache release archives and checksums.
The same cache can also be intentionally pre-seeded with custom builds, so
DevClean inventories it without assuming every archive is safely disposable.
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
class ElectronRootSet:
    active_cache_roots: tuple[PureWindowsPath, ...]
    legacy_cache_roots: tuple[PureWindowsPath, ...]
    relative_cache_override: bool


_ELECTRON_CACHE_RULE = ApplicationCleanupRule(
    rule_id="electron-download-cache-mixed",
    app_id="electron",
    root_key="ELECTRON_CACHE",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Electron download cache; may contain user-supplied custom builds",
)
_ELECTRON_LEGACY_CACHE_RULE = ApplicationCleanupRule(
    rule_id="electron-legacy-cache-mixed",
    app_id="electron",
    root_key="ELECTRON_LEGACY_CACHE",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.HIGH,
    label="Legacy Electron download cache; contents are not assumed disposable",
)

ELECTRON_RULES: tuple[ApplicationCleanupRule, ...] = (
    _ELECTRON_CACHE_RULE,
    _ELECTRON_LEGACY_CACHE_RULE,
)


def electron_roots(
    environment: Mapping[str, str] | None = None,
) -> ElectronRootSet:
    env = _casefold_env(environment)
    profile = env.get("userprofile")
    local = env.get("localappdata")
    configured = env.get("electron_config_cache")
    relative_override = False

    active: PureWindowsPath | None = None
    if configured:
        candidate = PureWindowsPath(_expand_home(configured, profile))
        if candidate.is_absolute():
            active = candidate
        else:
            relative_override = True
    elif local:
        active = PureWindowsPath(local) / "electron" / "Cache"
    elif profile:
        active = PureWindowsPath(profile) / "AppData" / "Local" / "electron" / "Cache"

    legacy = None if not profile else PureWindowsPath(profile) / ".electron"
    active_roots = _tuple_if_path(active)
    legacy_roots = tuple(
        root
        for root in _tuple_if_path(legacy)
        if all(_impl._normalize(root) != _impl._normalize(item) for item in active_roots)
    )
    return ElectronRootSet(
        active_cache_roots=active_roots,
        legacy_cache_roots=legacy_roots,
        relative_cache_override=relative_override,
    )


def electron_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = electron_roots(environment)
    return tuple(dict.fromkeys((*roots.active_cache_roots, *roots.legacy_cache_roots)))


def match_electron_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = electron_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    for candidates, rule in (
        (roots.active_cache_roots, _ELECTRON_CACHE_RULE),
        (roots.legacy_cache_roots, _ELECTRON_LEGACY_CACHE_RULE),
    ):
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
                matches.append((len(normalized_root), rule))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_electron_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_electron_rule(path, environment)
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


def electron_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_electron_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def electron_process_running() -> bool:
    """No mutation path is exposed, so no process gate is required yet."""

    return False


def clear_electron_process_cache() -> None:
    return None


def _expand_home(value: str, profile: str | None) -> str:
    if value == "~":
        return profile or value
    if value.startswith(("~/", "~\\")) and profile:
        return str(PureWindowsPath(profile) / value[2:])
    return value


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "ELECTRON_RULES",
    "ElectronRootSet",
    "clear_electron_process_cache",
    "electron_audited_tool_roots",
    "electron_process_running",
    "electron_roots",
    "electron_scan_roots",
    "evaluate_electron_path",
    "match_electron_rule",
    "whole_tree_electron_rule",
]
