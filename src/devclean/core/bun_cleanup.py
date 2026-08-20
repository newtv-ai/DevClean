"""Audited Bun package-manager storage semantics for Windows cleanup.

Bun's global module cache is rebuildable package-manager state, but current
``bun pm cache rm`` has broader lifecycle effects than DevClean's former raw
whole-tree rule: it also clears Bunx temporary caches and the cache can contain
a global virtual store used directly by project symlinks. Machine cache roots
remain visible but protected from generic deletion pending a dedicated vendor
USER_REVIEW lane.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
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
class BunRootSet:
    home_roots: tuple[PureWindowsPath, ...]
    cache_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_key: str,
    user_age_buckets: tuple[int, ...] = (),
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="bun",
        root_key=root_key,
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        user_age_buckets=user_age_buckets,
        label=label,
    )


_BUN_CACHE_RULE = _rule(
    "bun-global-module-cache",
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Bun global module cache/global store; generic raw deletion removed",
    root_key="BUN_CACHE",
)
_BUN_HOME_RULE = _rule(
    "bun-home-state",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Bun runtime, global installations, binaries and package-manager state",
    root_key="BUN_HOME",
)
_BUN_PROJECT_CACHE_RULE = _rule(
    "bun-project-cache",
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Project-local or custom Bun dependency cache",
    root_key="ANYWHERE",
    user_age_buckets=(30, 90, 180),
)
_BUN_PROJECT_METADATA_RULE = _rule(
    "bun-project-metadata",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Bun project lockfile and configuration",
    root_key="ANYWHERE",
)

BUN_RULES: tuple[ApplicationCleanupRule, ...] = (
    _BUN_CACHE_RULE,
    _BUN_HOME_RULE,
    _BUN_PROJECT_CACHE_RULE,
    _BUN_PROJECT_METADATA_RULE,
)

_PROJECT_METADATA_NAMES = frozenset({"bun.lock", "bun.lockb", "bunfig.toml"})


def bun_roots(environment: Mapping[str, str] | None = None) -> BunRootSet:
    env = _casefold_env(environment)
    homes: list[PureWindowsPath] = []
    caches: list[PureWindowsPath] = []

    explicit_home = env.get("devclean_bun_home")
    if explicit_home:
        _append_absolute(homes, explicit_home)
    else:
        userprofile = env.get("userprofile")
        if userprofile:
            homes.append(PureWindowsPath(userprofile) / ".bun")

    explicit_cache = env.get("devclean_bun_cache_dir")
    configured_cache = env.get("bun_install_cache_dir")
    if explicit_cache:
        _append_absolute(caches, explicit_cache)
    if configured_cache:
        _append_absolute(caches, configured_cache)

    # Documented default global package cache. A bunfig.toml can redirect the
    # effective cache for a project; DevClean deliberately does not execute Bun
    # or project configuration merely to discover another destructive root.
    for home in tuple(homes):
        caches.append(home / "install" / "cache")

    return BunRootSet(
        home_roots=_unique_paths(homes),
        cache_roots=_unique_paths(caches),
    )


def bun_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = bun_roots(environment)
    return tuple(dict.fromkeys((*roots.home_roots, *roots.cache_roots)))


def match_bun_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    candidate = PureWindowsPath(str(path))
    if candidate.name.casefold() in _PROJECT_METADATA_NAMES:
        return _BUN_PROJECT_METADATA_RULE

    normalized = _impl._normalize(path)
    roots = bun_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.cache_roots:
        _append_root_match(matches, normalized, root, _BUN_CACHE_RULE, 0)
    for root in roots.home_roots:
        _append_root_match(matches, normalized, root, _BUN_HOME_RULE, 10_000)
    if matches:
        return max(matches, key=lambda item: (item[0], item[1]))[2]

    parts = tuple(part.casefold() for part in candidate.parts)
    for index, part in enumerate(parts[:-1]):
        if part == ".bun" and parts[index + 1] == "cache":
            return _BUN_PROJECT_CACHE_RULE
    return None


def bun_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """No generic Bun delete root is currently source-authorized."""

    del environment
    return ()


def whole_tree_bun_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Raw whole-tree Bun cache deletion is deliberately not authorized."""

    del path, environment
    return None


def evaluate_bun_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del process_running
    rule = match_bun_rule(path, environment)
    if rule is None:
        return None

    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0)
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.USER_DECISION,
        observed,
        idle,
        None,
        _impl._benefit_score(logical_size, idle, None, rule.rebuild_cost),
        _impl._age_bucket(idle, rule.user_age_buckets),
    )


@lru_cache(maxsize=1)
def bun_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'bun.exe' }; if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def clear_bun_process_cache() -> None:
    bun_process_running.cache_clear()


def _append_root_match(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
    index: int,
) -> None:
    normalized_root = _impl._normalize(root)
    if not _impl._matches(normalized_path, normalized_root, MatchKind.PREFIX):
        return
    owner_weight = 3 if rule.owner is DecisionOwner.KEEP else 1
    matches.append((len(normalized_root), owner_weight * 1000 - index, rule))


def _append_absolute(found: list[PureWindowsPath], value: str) -> None:
    candidate = PureWindowsPath(value)
    if candidate.is_absolute():
        found.append(candidate)


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = _impl._normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(path)
    return tuple(found)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "BUN_RULES",
    "BunRootSet",
    "bun_audited_tool_roots",
    "bun_process_running",
    "bun_roots",
    "bun_scan_roots",
    "clear_bun_process_cache",
    "evaluate_bun_path",
    "match_bun_rule",
    "whole_tree_bun_rule",
]
