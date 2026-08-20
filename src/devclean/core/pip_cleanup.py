r"""Audited pip cache semantics for Windows cleanup.

pip documents its cache as disposable performance data and provides ``pip cache
dir`` / ``pip cache purge`` as the supported management interface. It also says
the internal filesystem layout is an implementation detail. DevClean therefore
inventories exact effective cache roots here but grants no generic raw file or
whole-tree deletion authority; mutation is delegated to pip in
``pip_maintenance``.
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
class PipRootSet:
    managed_cache_roots: tuple[PureWindowsPath, ...]
    custom_cache_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    rebuild_cost: RebuildCost,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="pip",
        root_key=root_key,
        relative_pattern="",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_PIP_MANAGED_CACHE_RULE = _rule(
    "pip-default-cache",
    root_key="PIP_CACHE",
    label="pip wheel and HTTP cache; maintain with pip cache purge",
    rebuild_cost=RebuildCost.MEDIUM,
)
_PIP_CUSTOM_CACHE_RULE = _rule(
    "pip-custom-cache",
    root_key="PIP_CUSTOM_CACHE",
    label="Custom pip cache directory; maintain with pip cache purge",
    rebuild_cost=RebuildCost.MEDIUM,
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

    # Explicit hook is treated as another dedicated cache root for tests and
    # controlled deployments. It still has no raw deletion authority.
    explicit = env.get("devclean_pip_cache_dir")
    if explicit:
        _append_absolute(managed, explicit)

    configured = env.get("pip_cache_dir")
    if configured:
        _append_absolute(custom, configured)

    # ``pip cache dir`` is pip's authoritative report of the active cache and
    # captures configuration files that static discovery does not parse.
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
    matches: list[tuple[int, ApplicationCleanupRule]] = []

    for root in roots.managed_cache_roots:
        _append_root_match(matches, normalized, root, _PIP_MANAGED_CACHE_RULE)
    for root in roots.custom_cache_roots:
        _append_root_match(matches, normalized, root, _PIP_CUSTOM_CACHE_RULE)

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def pip_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_pip_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
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
    del logical_size, process_running
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
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


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
    for command in pip_command_candidates():
        try:
            result = subprocess.run(
                [*command, "cache", "dir"],
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


def pip_command_candidates() -> tuple[tuple[str, ...], ...]:
    if os.name == "nt":
        return (
            ("py.exe", "-m", "pip"),
            ("python.exe", "-m", "pip"),
            ("pip.exe",),
        )
    return (
        ("python3", "-m", "pip"),
        ("python", "-m", "pip"),
        ("pip",),
    )


def clear_pip_process_cache() -> None:
    pip_process_running.cache_clear()
    _active_pip_cache_dir.cache_clear()


def _append_root_match(
    matches: list[tuple[int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
) -> None:
    normalized_root = _impl._normalize(root)
    if _impl._matches(normalized_path, normalized_root, MatchKind.PREFIX):
        matches.append((len(normalized_root), rule))


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
    "pip_command_candidates",
    "pip_process_running",
    "pip_roots",
    "pip_scan_roots",
    "whole_tree_pip_rule",
]
