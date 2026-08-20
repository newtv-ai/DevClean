r"""Audited Docker Desktop storage semantics for Windows cleanup.

Docker Desktop's WSL data directory contains an opaque Linux data disk holding
containers, images, volumes, and build cache together. DevClean inventories that
mixed storage but never grants raw filesystem deletion authority. Build-cache
maintenance is delegated separately to Docker's own CLI.
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
class DockerRootSet:
    desktop_data_roots: tuple[PureWindowsPath, ...]
    cli_config_roots: tuple[PureWindowsPath, ...]
    desktop_settings_paths: tuple[PureWindowsPath, ...]


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
        app_id="docker",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_DOCKER_DESKTOP_DATA_RULE = _rule(
    "docker-desktop-data-mixed",
    root_key="DOCKER_DESKTOP_DATA",
    label="Docker Desktop mixed container/image/volume/build-cache storage",
    rebuild_cost=RebuildCost.HIGH,
)
_DOCKER_CLI_CONFIG_RULE = _rule(
    "docker-cli-configuration",
    root_key="DOCKER_CONFIG",
    label="Docker CLI configuration, contexts, certificates and credentials",
    rebuild_cost=RebuildCost.HIGH,
)
_DOCKER_DESKTOP_SETTINGS_RULE = _rule(
    "docker-desktop-settings",
    root_key="DOCKER_DESKTOP_SETTINGS",
    label="Docker Desktop persistent settings",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)

DOCKER_RULES: tuple[ApplicationCleanupRule, ...] = (
    _DOCKER_DESKTOP_DATA_RULE,
    _DOCKER_CLI_CONFIG_RULE,
    _DOCKER_DESKTOP_SETTINGS_RULE,
)


def docker_roots(environment: Mapping[str, str] | None = None) -> DockerRootSet:
    env = _casefold_env(environment)
    userprofile = env.get("userprofile")
    localappdata = env.get("localappdata")
    appdata = env.get("appdata")

    desktop_data = PureWindowsPath(localappdata) / "Docker" / "wsl" if localappdata else None
    explicit_config = _absolute_path(env.get("docker_config"))
    cli_config = explicit_config
    if cli_config is None and userprofile:
        cli_config = PureWindowsPath(userprofile) / ".docker"
    desktop_settings = (
        PureWindowsPath(appdata) / "Docker" / "settings-store.json" if appdata else None
    )

    return DockerRootSet(
        desktop_data_roots=_tuple_if_path(desktop_data),
        cli_config_roots=_tuple_if_path(cli_config),
        desktop_settings_paths=_tuple_if_path(desktop_settings),
    )


def docker_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return only the documented Docker Desktop WSL storage anchor."""

    return docker_roots(environment).desktop_data_roots


def match_docker_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = docker_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []
    groups = (
        (roots.desktop_settings_paths, _DOCKER_DESKTOP_SETTINGS_RULE, MatchKind.EXACT),
        (roots.cli_config_roots, _DOCKER_CLI_CONFIG_RULE, MatchKind.PREFIX),
        (roots.desktop_data_roots, _DOCKER_DESKTOP_DATA_RULE, MatchKind.PREFIX),
    )
    for candidates, rule, match_kind in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, match_kind):
                matches.append((len(normalized_root), rule))
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_docker_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_docker_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    return ApplicationPolicyDecision(
        rule,
        PolicyAction.KEEP_PROTECTED,
        observed,
        idle,
        None,
        0,
    )


def docker_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_docker_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def docker_process_running() -> bool:
    """Return whether a Docker/BuildKit client build appears active.

    Docker Desktop itself must stay running for vendor maintenance commands, so
    the guard intentionally targets build clients rather than the daemon/UI.
    """

    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "(($_.Name -match '(?i)^docker\\.exe$') -and "
        "$_.CommandLine -match '(?i)(?:^|\\s)build(?:x)?(?:\\s|$)') -or "
        "$_.Name -match '(?i)^buildctl\\.exe$' "
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


def docker_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    override = env.get("devclean_docker_exe")
    if override:
        return override
    return "docker.exe" if os.name == "nt" else "docker"


def clear_docker_process_cache() -> None:
    docker_process_running.cache_clear()


def _absolute_path(value: str | None) -> PureWindowsPath | None:
    if not value:
        return None
    candidate = PureWindowsPath(value)
    return candidate if candidate.is_absolute() else None


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "DOCKER_RULES",
    "DockerRootSet",
    "clear_docker_process_cache",
    "docker_audited_tool_roots",
    "docker_executable",
    "docker_process_running",
    "docker_roots",
    "docker_scan_roots",
    "evaluate_docker_path",
    "match_docker_rule",
    "whole_tree_docker_rule",
]
