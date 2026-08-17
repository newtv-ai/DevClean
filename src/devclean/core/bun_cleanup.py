"""Audited Bun package-manager storage semantics for Windows cleanup.

Bun explicitly exposes a global module cache and a cache-removal command. The
surrounding ``~/.bun`` tree also contains the Bun executable, global packages,
linked binaries and other persistent package-manager state, so only the exact
cache root receives whole-tree TOOL authority. Project-local or otherwise custom
``.bun/cache`` directories are protected unless Bun itself reports them as the
active global cache.
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
    effective_idle_days,
)

_MIB = 1024**2


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
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    allow_whole_tree: bool = False,
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
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=allow_whole_tree,
        user_age_buckets=user_age_buckets,
        label=label,
    )


_BUN_CACHE_RULE = _rule(
    "bun-global-module-cache",
    DecisionOwner.TOOL,
    RebuildCost.MEDIUM,
    "Bun global module cache",
    root_key="BUN_CACHE",
    idle_days=30,
    min_reclaim_bytes=64 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
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

    # The documented default global module cache is ~/.bun/install/cache. Keep
    # it source-shaped even if Bun is not currently on PATH; the catalog still
    # requires the directory to exist before it becomes a cleanup root.
    for home in tuple(homes):
        caches.append(home / "install" / "cache")

    # ``bun pm cache`` is Bun's authoritative way to print the effective global
    # module cache and therefore captures bunfig/CLI/runtime configuration that
    # static path guessing cannot safely reproduce.
    if environment is None:
        live = _active_bun_cache()
        if live:
            _append_absolute(caches, live)

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

    # A direct project ``.bun/cache`` is intentionally not assumed to be the
    # global disposable cache. Users can point Bun's cache at arbitrary paths,
    # including project paths kept for CI/offline reuse.
    parts = tuple(part.casefold() for part in candidate.parts)
    for index, part in enumerate(parts[:-1]):
        if part == ".bun" and parts[index + 1] == "cache":
            return _BUN_PROJECT_CACHE_RULE
    return None


def bun_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for root in bun_roots(environment).cache_roots:
        key = _impl._normalize(root)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append((root, _BUN_CACHE_RULE))
    return tuple(found)


def whole_tree_bun_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in bun_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
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
    rule = match_bun_rule(path, environment)
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
    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(
            rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0
        )
    if rule.owner is DecisionOwner.USER:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.USER_DECISION,
            observed,
            idle,
            None,
            _impl._benefit_score(logical_size, idle, None, rule.rebuild_cost),
            _impl._age_bucket(idle, rule.user_age_buckets),
        )

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = bun_process_running()
    score = _impl._benefit_score(logical_size, idle, threshold, rule.rebuild_cost)
    if rule.requires_process_closed and running:
        action = PolicyAction.TOOL_KEEP_IN_USE
    elif logical_size < rule.min_reclaim_bytes:
        action = PolicyAction.TOOL_KEEP_LOW_BENEFIT
    elif idle is None or threshold is None:
        action = PolicyAction.TOOL_KEEP_UNKNOWN_USAGE
    elif idle < threshold:
        action = PolicyAction.TOOL_KEEP_RECENT
    else:
        action = PolicyAction.TOOL_DELETE
    return ApplicationPolicyDecision(rule, action, observed, idle, threshold, score)


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


@lru_cache(maxsize=1)
def _active_bun_cache() -> str | None:
    executable = "bun.exe" if os.name == "nt" else "bun"
    try:
        result = subprocess.run(
            [executable, "pm", "cache"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    return lines[-1].strip().strip('"').strip("'")


def clear_bun_process_cache() -> None:
    bun_process_running.cache_clear()
    _active_bun_cache.cache_clear()


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
