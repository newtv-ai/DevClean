r"""Audited Maven local-repository semantics for Windows cleanup.

Maven's local repository is intentionally treated as mixed persistent storage:
it caches remote artifacts but also contains locally built and installed artifacts.
Maven explicitly warns consumers not to manipulate the repository with plain file
operations because resolver implementations, locking, and split layouts vary.
DevClean therefore inventories the effective local repository but grants it no
generic deletion authority.
"""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath

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
class MavenRootSet:
    local_repository_roots: tuple[PureWindowsPath, ...]
    user_config_roots: tuple[PureWindowsPath, ...]
    config_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    label: str,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="maven",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.HIGH,
        label=label,
    )


_MAVEN_LOCAL_REPOSITORY_RULE = _rule(
    "maven-local-repository-mixed",
    root_key="MAVEN_LOCAL_REPOSITORY",
    label="Maven local repository: remote cache mixed with locally installed artifacts",
)
_MAVEN_USER_CONFIG_ROOT_RULE = _rule(
    "maven-user-config-root",
    root_key="MAVEN_USER_CONFIG",
    label="Maven user configuration and persistent state",
)
_MAVEN_SETTINGS_RULE = _rule(
    "maven-settings",
    root_key="MAVEN_CONFIG",
    label="Maven settings, repositories, proxies and credentials",
    match_kind=MatchKind.EXACT,
)
_MAVEN_TOOLCHAINS_RULE = _rule(
    "maven-toolchains",
    root_key="MAVEN_CONFIG",
    label="Maven user toolchain configuration",
    match_kind=MatchKind.EXACT,
)
_MAVEN_SECURITY_RULE = _rule(
    "maven-security-settings",
    root_key="MAVEN_CONFIG",
    label="Maven encrypted-credential security settings",
    match_kind=MatchKind.EXACT,
)
_MAVEN_PROJECT_METADATA_RULE = _rule(
    "maven-project-metadata",
    root_key="ANYWHERE",
    label="Maven project build/dependency metadata",
    match_kind=MatchKind.EXACT,
)
_MAVEN_PROJECT_CONFIG_RULE = _rule(
    "maven-project-configuration",
    root_key="ANYWHERE",
    label="Project-local Maven wrapper/configuration",
    match_kind=MatchKind.EXACT,
)

MAVEN_RULES: tuple[ApplicationCleanupRule, ...] = (
    _MAVEN_LOCAL_REPOSITORY_RULE,
    _MAVEN_USER_CONFIG_ROOT_RULE,
    _MAVEN_SETTINGS_RULE,
    _MAVEN_TOOLCHAINS_RULE,
    _MAVEN_SECURITY_RULE,
    _MAVEN_PROJECT_METADATA_RULE,
    _MAVEN_PROJECT_CONFIG_RULE,
)

_PROJECT_METADATA_NAMES = frozenset({"pom.xml"})
_PROJECT_MVN_FILES = frozenset(
    {
        "maven.config",
        "jvm.config",
        "extensions.xml",
        "maven-wrapper.properties",
        "maven-wrapper.jar",
    }
)
_PROPERTY_PATTERN = re.compile(r"\$\{([^}]+)\}")
_REPO_ARG_PATTERN = re.compile(
    r"(?:^|\s)-Dmaven\.repo\.local=(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
    re.IGNORECASE,
)


def maven_roots(environment: Mapping[str, str] | None = None) -> MavenRootSet:
    env = _casefold_env(environment)
    home = env.get("userprofile")
    if not home:
        return MavenRootSet((), (), ())

    home_path = PureWindowsPath(home)
    user_config = _first_absolute(
        env.get("devclean_maven_user_conf"),
        _extract_user_property(env.get("maven_args"), "maven.user.conf"),
        _extract_user_property(env.get("maven_opts"), "maven.user.conf"),
    ) or home_path / ".m2"

    settings_path = user_config / "settings.xml"
    explicit_repo = _first_absolute(
        env.get("devclean_maven_repo_local"),
        _extract_repo_argument(env.get("maven_args")),
        _extract_repo_argument(env.get("maven_opts")),
    )
    configured_repo = (
        _repository_from_settings(Path(str(settings_path)), environment)
        if explicit_repo is None
        else None
    )
    repository = explicit_repo or configured_repo or user_config / "repository"

    config_paths = (
        settings_path,
        user_config / "toolchains.xml",
        user_config / "settings-security.xml",
        user_config / "settings-security4.xml",
        user_config / "extensions.xml",
    )
    return MavenRootSet(
        local_repository_roots=(repository,),
        user_config_roots=(user_config,),
        config_paths=tuple(dict.fromkeys(config_paths)),
    )


def maven_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return maven_roots(environment).local_repository_roots


def match_maven_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = maven_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []

    for root in roots.local_repository_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            matches.append((len(normalized_root), _MAVEN_LOCAL_REPOSITORY_RULE))

    for root in roots.user_config_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            matches.append((len(normalized_root), _MAVEN_USER_CONFIG_ROOT_RULE))

    for config_path in roots.config_paths:
        normalized_config = _impl._normalize(config_path)
        if normalized != normalized_config:
            continue
        name = PureWindowsPath(config_path).name.casefold()
        if name == "settings.xml":
            rule = _MAVEN_SETTINGS_RULE
        elif name == "toolchains.xml":
            rule = _MAVEN_TOOLCHAINS_RULE
        elif name.startswith("settings-security"):
            rule = _MAVEN_SECURITY_RULE
        else:
            rule = _MAVEN_USER_CONFIG_ROOT_RULE
        matches.append((len(normalized_config), rule))

    windows_path = PureWindowsPath(str(path))
    name = windows_path.name.casefold()
    if name in _PROJECT_METADATA_NAMES:
        matches.append((len(normalized), _MAVEN_PROJECT_METADATA_RULE))
    if any(part.casefold() == ".mvn" for part in windows_path.parts) and (
        name in _PROJECT_MVN_FILES
    ):
        matches.append((len(normalized), _MAVEN_PROJECT_CONFIG_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_maven_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_maven_rule(path, environment)
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


def maven_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_maven_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def maven_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^(?:mvn|mvnd)\\.exe$' -or "
        "(($_.Name -match '(?i)^java(?:w)?\\.exe$') -and "
        "$_.CommandLine -match '(?i)(?:plexus\\.classworlds|maven|mvnd)') "
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


def clear_maven_process_cache() -> None:
    maven_process_running.cache_clear()


def _repository_from_settings(
    settings_path: Path,
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    try:
        if not settings_path.is_file():
            return None
        root = ET.parse(settings_path).getroot()
    except (OSError, ET.ParseError):
        return None
    local_repo: str | None = None
    for child in root:
        if child.tag.rsplit("}", 1)[-1] != "localRepository":
            continue
        if child.text and child.text.strip():
            local_repo = child.text.strip()
        break
    if not local_repo:
        return None
    expanded = _expand_settings_value(local_repo, environment)
    return _absolute_path(expanded)


def _expand_settings_value(
    value: str,
    environment: Mapping[str, str] | None,
) -> str:
    source = os.environ if environment is None else environment
    env = {key.casefold(): item for key, item in source.items()}

    def replacement(match: re.Match[str]) -> str:
        token = match.group(1)
        folded = token.casefold()
        if folded == "user.home":
            return env.get("userprofile", match.group(0))
        if folded.startswith("env."):
            return env.get(folded[4:], match.group(0))
        return match.group(0)

    return _PROPERTY_PATTERN.sub(replacement, value)


def _extract_repo_argument(value: str | None) -> str | None:
    if not value:
        return None
    match = _REPO_ARG_PATTERN.search(value)
    if match is None:
        return None
    return next((group for group in match.groups() if group), None)


def _extract_user_property(value: str | None, name: str) -> str | None:
    if not value:
        return None
    pattern = re.compile(
        rf"(?:^|\s)-D{re.escape(name)}=(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
        re.IGNORECASE,
    )
    match = pattern.search(value)
    if match is None:
        return None
    return next((group for group in match.groups() if group), None)


def _absolute_path(value: str | None) -> PureWindowsPath | None:
    if not value or "${" in value:
        return None
    candidate = PureWindowsPath(value)
    return candidate if candidate.is_absolute() else None


def _first_absolute(*values: str | None) -> PureWindowsPath | None:
    for value in values:
        candidate = _absolute_path(value)
        if candidate is not None:
            return candidate
    return None


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "MAVEN_RULES",
    "MavenRootSet",
    "clear_maven_process_cache",
    "evaluate_maven_path",
    "match_maven_rule",
    "maven_audited_tool_roots",
    "maven_process_running",
    "maven_roots",
    "maven_scan_roots",
    "whole_tree_maven_rule",
]
