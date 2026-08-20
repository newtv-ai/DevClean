r"""Audited Go toolchain storage semantics for Windows cleanup.

The Go command exposes its effective build and module cache directories via
``go env`` and provides supported cleanup commands for both. DevClean inventories
those exact locations but grants no generic raw deletion authority. Persistent
Go configuration, installed binaries, and project module metadata remain
protected.
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
class GoRootSet:
    build_cache_roots: tuple[PureWindowsPath, ...]
    module_cache_roots: tuple[PureWindowsPath, ...]
    install_bin_roots: tuple[PureWindowsPath, ...]
    config_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    rebuild_cost: RebuildCost,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="go",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_GO_BUILD_CACHE_RULE = _rule(
    "go-build-cache-vendor-managed",
    root_key="GO_BUILD_CACHE",
    label="Go build/test cache; maintain with go clean -cache",
    rebuild_cost=RebuildCost.LOW,
)
_GO_MODULE_CACHE_RULE = _rule(
    "go-module-cache-vendor-managed",
    root_key="GO_MODULE_CACHE",
    label="Go downloaded module cache; maintain with go clean -modcache",
    rebuild_cost=RebuildCost.HIGH,
)
_GO_INSTALL_BIN_RULE = _rule(
    "go-installed-binaries",
    root_key="GO_BIN",
    label="User-installed Go command binaries",
    rebuild_cost=RebuildCost.HIGH,
)
_GO_ENV_CONFIG_RULE = _rule(
    "go-environment-configuration",
    root_key="GO_ENV",
    label="Persistent Go environment configuration",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)
_GO_PROJECT_METADATA_RULE = _rule(
    "go-project-module-metadata",
    root_key="ANYWHERE",
    label="Go project module/workspace metadata",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)

GO_RULES: tuple[ApplicationCleanupRule, ...] = (
    _GO_BUILD_CACHE_RULE,
    _GO_MODULE_CACHE_RULE,
    _GO_INSTALL_BIN_RULE,
    _GO_ENV_CONFIG_RULE,
    _GO_PROJECT_METADATA_RULE,
)

_PROJECT_METADATA_NAMES = frozenset(
    {
        "go.mod",
        "go.sum",
        "go.work",
        "go.work.sum",
    }
)


def go_roots(environment: Mapping[str, str] | None = None) -> GoRootSet:
    env = _casefold_env(environment)
    discovered = _effective_go_env() if environment is None else {}

    home = env.get("userprofile")
    local = env.get("localappdata")
    appdata = env.get("appdata")

    build_cache = _first_absolute(
        env.get("devclean_go_cache"),
        env.get("gocache"),
        discovered.get("GOCACHE"),
    )
    if build_cache is None and local:
        build_cache = PureWindowsPath(local) / "go-build"
    if build_cache is not None and str(build_cache).casefold() == "off":
        build_cache = None

    gopath = _first_nonempty(
        env.get("devclean_go_path"),
        env.get("gopath"),
        discovered.get("GOPATH"),
    )
    gopath_roots = _split_windows_path_list(gopath)
    if not gopath_roots and home:
        gopath_roots = (PureWindowsPath(home) / "go",)

    module_cache = _first_absolute(
        env.get("devclean_go_modcache"),
        env.get("gomodcache"),
        discovered.get("GOMODCACHE"),
    )
    if module_cache is None and gopath_roots:
        module_cache = gopath_roots[0] / "pkg" / "mod"

    gobin = _first_absolute(
        env.get("devclean_go_bin"),
        env.get("gobin"),
        discovered.get("GOBIN"),
    )
    if gobin is None and gopath_roots:
        gobin = gopath_roots[0] / "bin"

    goenv_value = _first_nonempty(
        env.get("devclean_go_env"),
        env.get("goenv"),
        discovered.get("GOENV"),
    )
    goenv = _absolute_path(goenv_value)
    if goenv is None and appdata:
        goenv = PureWindowsPath(appdata) / "go" / "env"
    if goenv is not None and str(goenv).casefold() == "off":
        goenv = None

    return GoRootSet(
        build_cache_roots=_tuple_if_path(build_cache),
        module_cache_roots=_tuple_if_path(module_cache),
        install_bin_roots=_tuple_if_path(gobin),
        config_paths=_tuple_if_path(goenv),
    )


def go_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = go_roots(environment)
    return tuple(
        dict.fromkeys((*roots.build_cache_roots, *roots.module_cache_roots))
    )


def match_go_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = go_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []

    groups = (
        (roots.build_cache_roots, _GO_BUILD_CACHE_RULE, MatchKind.PREFIX),
        (roots.module_cache_roots, _GO_MODULE_CACHE_RULE, MatchKind.PREFIX),
        (roots.install_bin_roots, _GO_INSTALL_BIN_RULE, MatchKind.PREFIX),
        (roots.config_paths, _GO_ENV_CONFIG_RULE, MatchKind.EXACT),
    )
    for candidates, rule, match_kind in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, match_kind):
                matches.append((len(normalized_root), rule))

    name = PureWindowsPath(str(path)).name.casefold()
    if name in _PROJECT_METADATA_NAMES:
        matches.append((len(normalized), _GO_PROJECT_METADATA_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_go_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_go_rule(path, environment)
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


def go_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_go_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def go_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:go|gopls)\\.exe$' "
        "}; if ($p) { 'RUNNING' }"
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
def _effective_go_env() -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                go_executable(),
                "env",
                "-json",
                "GOCACHE",
                "GOMODCACHE",
                "GOENV",
                "GOPATH",
                "GOBIN",
            ],
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
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items() if isinstance(value, str)}


def go_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    override = env.get("devclean_go_exe")
    if override:
        return override
    return "go.exe" if os.name == "nt" else "go"


def clear_go_process_cache() -> None:
    go_process_running.cache_clear()
    _effective_go_env.cache_clear()


def _absolute_path(value: str | None) -> PureWindowsPath | None:
    if not value:
        return None
    candidate = PureWindowsPath(value)
    return candidate if candidate.is_absolute() else None


def _first_absolute(*values: str | None) -> PureWindowsPath | None:
    for value in values:
        candidate = _absolute_path(value)
        if candidate is not None:
            return candidate
    return None


def _first_nonempty(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def _split_windows_path_list(value: str | None) -> tuple[PureWindowsPath, ...]:
    if not value:
        return ()
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for raw in value.split(";"):
        candidate = _absolute_path(raw.strip())
        if candidate is None:
            continue
        key = _impl._normalize(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(candidate)
    return tuple(found)


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "GO_RULES",
    "GoRootSet",
    "clear_go_process_cache",
    "evaluate_go_path",
    "go_audited_tool_roots",
    "go_executable",
    "go_process_running",
    "go_roots",
    "go_scan_roots",
    "match_go_rule",
    "whole_tree_go_rule",
]
