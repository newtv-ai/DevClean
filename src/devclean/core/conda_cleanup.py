r"""Audited Conda storage semantics for Windows cleanup.

Conda package caches are caches, but extracted package directories may be the
link source for installed environments. Conda itself warns that removing package
cache contents can break environments that use symlinks. DevClean therefore
inventories exact effective package-cache roots but grants no raw file or
whole-tree deletion authority. Cache maintenance is delegated to ``conda clean``.
"""

from __future__ import annotations

import json
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
class CondaRootSet:
    package_cache_roots: tuple[PureWindowsPath, ...]
    environment_roots: tuple[PureWindowsPath, ...]
    root_prefixes: tuple[PureWindowsPath, ...]
    state_roots: tuple[PureWindowsPath, ...]
    config_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    owner: DecisionOwner,
    label: str,
    rebuild_cost: RebuildCost,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="conda",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_CONDA_PACKAGE_CACHE_RULE = _rule(
    "conda-package-cache-vendor-managed",
    root_key="CONDA_PKGS",
    owner=DecisionOwner.KEEP,
    label="Conda package cache; maintain with conda clean instead of raw deletion",
    rebuild_cost=RebuildCost.HIGH,
)
_CONDA_ENVIRONMENTS_RULE = _rule(
    "conda-environments",
    root_key="CONDA_ENVS",
    owner=DecisionOwner.USER,
    label="User-created Conda environments",
    rebuild_cost=RebuildCost.HIGH,
)
_CONDA_ROOT_PREFIX_RULE = _rule(
    "conda-root-prefix",
    root_key="CONDA_ROOT",
    owner=DecisionOwner.KEEP,
    label="Conda base installation and persistent runtime state",
    rebuild_cost=RebuildCost.HIGH,
)
_CONDA_STATE_RULE = _rule(
    "conda-user-state",
    root_key="CONDA_STATE",
    owner=DecisionOwner.KEEP,
    label="Conda user state, environment registry and credentials",
    rebuild_cost=RebuildCost.HIGH,
)
_CONDA_CONFIG_RULE = _rule(
    "conda-configuration",
    root_key="CONDA_CONFIG",
    owner=DecisionOwner.KEEP,
    label="Conda runtime configuration",
    rebuild_cost=RebuildCost.HIGH,
)
_CONDA_ENV_METADATA_RULE = _rule(
    "conda-environment-metadata",
    root_key="ANYWHERE",
    owner=DecisionOwner.KEEP,
    label="Conda environment metadata",
    rebuild_cost=RebuildCost.HIGH,
)

CONDA_RULES: tuple[ApplicationCleanupRule, ...] = (
    _CONDA_PACKAGE_CACHE_RULE,
    _CONDA_ENVIRONMENTS_RULE,
    _CONDA_ROOT_PREFIX_RULE,
    _CONDA_STATE_RULE,
    _CONDA_CONFIG_RULE,
    _CONDA_ENV_METADATA_RULE,
)


def conda_roots(environment: Mapping[str, str] | None = None) -> CondaRootSet:
    env = _casefold_env(environment)
    package_caches: list[PureWindowsPath] = []
    environment_roots: list[PureWindowsPath] = []
    root_prefixes: list[PureWindowsPath] = []
    state_roots: list[PureWindowsPath] = []
    config_paths: list[PureWindowsPath] = []

    home = env.get("userprofile")
    if home:
        home_path = PureWindowsPath(home)
        state_roots.append(home_path / ".conda")
        config_paths.append(home_path / ".condarc")

    programdata = env.get("programdata")
    if programdata:
        config_paths.append(PureWindowsPath(programdata) / "conda")

    package_value = env.get("devclean_conda_pkgs_dirs") or env.get("conda_pkgs_dirs")
    if package_value:
        for value in _split_path_list(package_value, separators=",;"):
            _append_absolute(package_caches, value)

    envs_value = env.get("devclean_conda_envs_dirs") or env.get("conda_envs_path")
    if envs_value:
        for value in _split_path_list(envs_value, separators=";"):
            _append_absolute(environment_roots, value)

    root_value = env.get("devclean_conda_root_prefix")
    if root_value:
        _append_absolute(root_prefixes, root_value)

    info = _effective_conda_info() if environment is None else {}
    for value in _info_path_list(info, "pkgs_dirs"):
        _append_absolute(package_caches, value)
    for value in _info_path_list(info, "envs_dirs"):
        _append_absolute(environment_roots, value)
    root_prefix_value = info.get("root_prefix")
    if isinstance(root_prefix_value, str):
        _append_absolute(root_prefixes, root_prefix_value)

    if not root_prefixes:
        inferred_root = _root_from_conda_executable(env)
        if inferred_root is not None:
            root_prefixes.append(inferred_root)

    if not package_caches:
        for root in root_prefixes:
            package_caches.append(root / "pkgs")
    if not environment_roots:
        for root in root_prefixes:
            environment_roots.append(root / "envs")

    return CondaRootSet(
        package_cache_roots=_unique_paths(package_caches),
        environment_roots=_unique_paths(environment_roots),
        root_prefixes=_unique_paths(root_prefixes),
        state_roots=_unique_paths(state_roots),
        config_paths=_unique_paths(config_paths),
    )


def conda_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Inventory only package-cache roots; environments remain semantic guards."""

    return conda_roots(environment).package_cache_roots


def match_conda_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = conda_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    groups = (
        (roots.package_cache_roots, _CONDA_PACKAGE_CACHE_RULE, MatchKind.PREFIX),
        (roots.environment_roots, _CONDA_ENVIRONMENTS_RULE, MatchKind.PREFIX),
        (roots.root_prefixes, _CONDA_ROOT_PREFIX_RULE, MatchKind.PREFIX),
        (roots.state_roots, _CONDA_STATE_RULE, MatchKind.PREFIX),
        (roots.config_paths, _CONDA_CONFIG_RULE, MatchKind.PREFIX),
    )
    for candidates, rule, match_kind in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if not _impl._matches(normalized, normalized_root, match_kind):
                continue
            owner_weight = 2 if rule.owner is DecisionOwner.KEEP else 1
            matches.append((len(normalized_root), owner_weight, rule))

    windows_path = PureWindowsPath(str(path))
    if windows_path.name.casefold() in {".condarc", "condarc"}:
        matches.append((len(normalized), 2, _CONDA_CONFIG_RULE))
    if any(part.casefold() == "conda-meta" for part in windows_path.parts):
        matches.append((len(normalized), 3, _CONDA_ENV_METADATA_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def evaluate_conda_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_conda_rule(path, environment)
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
    action = (
        PolicyAction.USER_DECISION
        if rule.owner is DecisionOwner.USER
        else PolicyAction.KEEP_PROTECTED
    )
    return ApplicationPolicyDecision(rule, action, observed, idle, None, 0)


def conda_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_conda_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def conda_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:_?conda|mamba|micromamba)\\.exe$' -or "
        "(($_.Name -match '(?i)^(?:python|pythonw|py)(?:\\d+(?:\\.\\d+)?)?\\.exe$') "
        "-and $_.CommandLine -match '(?i)(?:^|\\s)-m\\s+conda(?:\\s|$)') }; "
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
def _effective_conda_info() -> dict[str, object]:
    executable = conda_executable()
    try:
        result = subprocess.run(
            [executable, "info", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def conda_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_conda_exe") or env.get("conda_exe")
    if configured:
        return configured
    return "conda.exe" if os.name == "nt" else "conda"


def clear_conda_process_cache() -> None:
    conda_process_running.cache_clear()
    _effective_conda_info.cache_clear()


def _root_from_conda_executable(env: Mapping[str, str]) -> PureWindowsPath | None:
    value = env.get("devclean_conda_exe") or env.get("conda_exe")
    if not value:
        return None
    candidate = PureWindowsPath(value)
    if not candidate.is_absolute():
        return None
    parent = candidate.parent
    if parent.name.casefold() not in {"scripts", "condabin"}:
        return None
    return parent.parent


def _info_path_list(info: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = info.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _split_path_list(value: str, *, separators: str) -> tuple[str, ...]:
    parts = [value]
    for separator in separators:
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(separator))
        parts = expanded
    return tuple(part.strip().strip('"').strip("'") for part in parts if part.strip())


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
    "CONDA_RULES",
    "CondaRootSet",
    "clear_conda_process_cache",
    "conda_audited_tool_roots",
    "conda_executable",
    "conda_process_running",
    "conda_roots",
    "conda_scan_roots",
    "evaluate_conda_path",
    "match_conda_rule",
    "whole_tree_conda_rule",
]
