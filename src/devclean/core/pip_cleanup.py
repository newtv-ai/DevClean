"""Audited pip cache semantics for Windows cleanup.

pip documents its default Windows cache as ``%LocalAppData%\pip\Cache`` and
provides ``pip cache dir`` / ``pip cache purge`` as the supported cache
management interface. The documented default is a dedicated cache root, so it
can receive exact whole-tree TOOL authority. A user-configured cache directory
is discovered for inventory but kept protected because pip treats the internal
layout as an implementation detail and a custom root can be intentionally
co-located with other user/CI state.
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
class PipRootSet:
    managed_cache_roots: tuple[PureWindowsPath, ...]
    custom_cache_roots: tuple[PureWindowsPath, ...]


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
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="pip",
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
        label=label,
    )


_PIP_MANAGED_CACHE_RULE = _rule(
    "pip-default-cache",
    DecisionOwner.TOOL,
    RebuildCost.MEDIUM,
    "pip default wheel and HTTP cache",
    root_key="PIP_CACHE",
    idle_days=30,
    min_reclaim_bytes=64 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
    allow_whole_tree=True,
)
_PIP_CUSTOM_CACHE_RULE = _rule(
    "pip-custom-cache",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Custom pip cache directory; maintain with pip cache purge",
    root_key="PIP_CUSTOM_CACHE",
)

PIP_RULES: tuple[ApplicationCleanupRule, ...] = (
    _PIP_MANAGED_CACHE_RULE,
    _PIP_CUSTOM_CACHE_RULE,
)


def pip_roots(environment: Mapping[str, str] | None = None) -> PipRootSet:
    env = _casefold_env(environment)
    managed: list[PureWindowsPath] = []
    custom: list[PureWindowsPath] = []

    localappdata = env.get("localappdata")
    if localappdata:
        managed.append(PureWindowsPath(localappdata) / "pip" / "Cache")

    # Explicit hook is intentionally treated as a dedicated default-shaped root
    # so tests and controlled deployments can relocate the audited cache safely.
    explicit = env.get("devclean_pip_cache_dir")
    if explicit:
        _append_absolute(managed, explicit)

    configured = env.get("pip_cache_dir")
    if configured:
        _append_absolute(custom, configured)

    # ``pip cache dir`` is pip's authoritative report of the effective cache
    # directory and captures pip configuration files that static discovery does
    # not parse. Non-default results stay KEEP because the custom root may be
    # shared with user or CI state and pip's internal layout is not stable API.
    if environment is None:
        active = _active_pip_cache_dir()
        if active:
            _append_absolute(custom, active)

    managed_roots = _unique_paths(managed)
    managed_keys = {_impl._normalize(root) for root in managed_roots}
    custom_roots = tuple(
        root
        for root in _unique_paths(custom)
        if _impl._normalize(root) not in managed_keys
    )
    return PipRootSet(
        managed_cache_roots=managed_roots,
        custom_cache_roots=custom_roots,
    )


def pip_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = pip_roots(environment)
    return tuple(
        dict.fromkeys((*roots.managed_cache_roots, *roots.custom_cache_roots))
    )


def match_pip_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = pip_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.managed_cache_roots:
        _append_root_match(matches, normalized, root, _PIP_MANAGED_CACHE_RULE, 0)
    for root in roots.custom_cache_roots:
        _append_root_match(matches, normalized, root, _PIP_CUSTOM_CACHE_RULE, 0)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def pip_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for root in pip_roots(environment).managed_cache_roots:
        key = _impl._normalize(root)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append((root, _PIP_MANAGED_CACHE_RULE))
    return tuple(found)


def whole_tree_pip_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in pip_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_pip_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_pip_rule(path, environment)
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

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = pip_process_running()
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
def pip_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^pip(?:\\d+(?:\\.\\d+)?)?\\.exe$' -or "
        "(($_.Name -match '(?i)^(?:python|pythonw|py)(?:\\d+(?:\\.\\d+)?)?\\.exe$') "
        "-and $_.CommandLine -match '(?i)(?:^|\\s)-m\\s+pip(?:\\s|$)') }; "
        "if ($p) { 'RUNNING' }"
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
def _active_pip_cache_dir() -> str | None:
    commands = (
        ("py.exe", "-m", "pip", "cache", "dir"),
        ("python.exe", "-m", "pip", "cache", "dir"),
        ("pip.exe", "cache", "dir"),
    ) if os.name == "nt" else (
        ("python3", "-m", "pip", "cache", "dir"),
        ("python", "-m", "pip", "cache", "dir"),
        ("pip", "cache", "dir"),
    )
    for command in commands:
        try:
            result = subprocess.run(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            continue
        candidate = lines[-1].strip().strip('"').strip("'")
        if PureWindowsPath(candidate).is_absolute():
            return candidate
    return None


def clear_pip_process_cache() -> None:
    pip_process_running.cache_clear()
    _active_pip_cache_dir.cache_clear()


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
    "PIP_RULES",
    "PipRootSet",
    "clear_pip_process_cache",
    "evaluate_pip_path",
    "match_pip_rule",
    "pip_audited_tool_roots",
    "pip_process_running",
    "pip_roots",
    "pip_scan_roots",
    "whole_tree_pip_rule",
]
