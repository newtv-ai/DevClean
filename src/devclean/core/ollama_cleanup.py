r"""Audited Ollama model-store semantics for Windows cleanup.

Ollama's model directory is user-selected downloaded content, not a disposable
cache. Its internal blobs can be shared by multiple model manifests, so DevClean
never grants raw file or whole-tree deletion authority. User-initiated model
removal belongs to Ollama's own ``ollama rm`` / delete API semantics.
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
class OllamaRootSet:
    home_roots: tuple[PureWindowsPath, ...]
    model_roots: tuple[PureWindowsPath, ...]
    configuration_paths: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    *,
    root_key: str,
    owner: DecisionOwner,
    label: str,
    match_kind: MatchKind = MatchKind.PREFIX,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="ollama",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.HIGH,
        label=label,
    )


_OLLAMA_MODELS_RULE = _rule(
    "ollama-model-store",
    root_key="OLLAMA_MODELS",
    owner=DecisionOwner.USER,
    label="User-selected Ollama downloaded model store",
)
_OLLAMA_HOME_RULE = _rule(
    "ollama-home-state",
    root_key="OLLAMA_HOME",
    owner=DecisionOwner.KEEP,
    label="Ollama home configuration and unknown persistent state",
)
_OLLAMA_SERVER_CONFIG_RULE = _rule(
    "ollama-server-configuration",
    root_key="OLLAMA_CONFIG",
    owner=DecisionOwner.KEEP,
    label="Ollama server configuration",
    match_kind=MatchKind.EXACT,
)

OLLAMA_RULES: tuple[ApplicationCleanupRule, ...] = (
    _OLLAMA_MODELS_RULE,
    _OLLAMA_HOME_RULE,
    _OLLAMA_SERVER_CONFIG_RULE,
)


def ollama_roots(environment: Mapping[str, str] | None = None) -> OllamaRootSet:
    env = _casefold_env(environment)
    userprofile = env.get("userprofile")
    if not userprofile:
        return OllamaRootSet((), (), ())

    home = PureWindowsPath(userprofile) / ".ollama"
    model_root = (
        _first_absolute(
            env.get("devclean_ollama_models"),
            env.get("ollama_models"),
        )
        or home / "models"
    )
    return OllamaRootSet(
        home_roots=(home,),
        model_roots=(model_root,),
        configuration_paths=(home / "server.json",),
    )


def ollama_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return ollama_roots(environment).model_roots


def match_ollama_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = ollama_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in roots.model_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            matches.append((len(normalized_root), 1, _OLLAMA_MODELS_RULE))

    for config in roots.configuration_paths:
        normalized_config = _impl._normalize(config)
        if normalized == normalized_config:
            matches.append((len(normalized_config), 3, _OLLAMA_SERVER_CONFIG_RULE))

    for root in roots.home_roots:
        normalized_root = _impl._normalize(root)
        if _impl._matches(normalized, normalized_root, MatchKind.PREFIX):
            matches.append((len(normalized_root), 2, _OLLAMA_HOME_RULE))

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def evaluate_ollama_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_ollama_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    action = (
        PolicyAction.USER_DECISION
        if rule.owner is DecisionOwner.USER
        else PolicyAction.KEEP_PROTECTED
    )
    return ApplicationPolicyDecision(rule, action, observed, idle, None, 0)


def ollama_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_ollama_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def ollama_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^ollama(?: app)?\\.exe$' "
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


def clear_ollama_process_cache() -> None:
    ollama_process_running.cache_clear()


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


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "OLLAMA_RULES",
    "OllamaRootSet",
    "clear_ollama_process_cache",
    "evaluate_ollama_path",
    "match_ollama_rule",
    "ollama_audited_tool_roots",
    "ollama_process_running",
    "ollama_roots",
    "ollama_scan_roots",
    "whole_tree_ollama_rule",
]
