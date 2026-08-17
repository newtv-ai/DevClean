r"""Audited Hugging Face Hub cache semantics for Windows cleanup.

``HF_HOME`` is mixed state: it can contain authentication tokens plus several
independent caches. DevClean inventories exact documented cache roots but never
recursively deletes Hugging Face storage itself. Hub cache garbage collection is
delegated to the supported ``hf cache prune`` command.
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
class HuggingFaceRootSet:
    home_roots: tuple[PureWindowsPath, ...]
    hub_cache_roots: tuple[PureWindowsPath, ...]
    xet_cache_roots: tuple[PureWindowsPath, ...]
    assets_cache_roots: tuple[PureWindowsPath, ...]
    token_paths: tuple[PureWindowsPath, ...]


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
        app_id="huggingface",
        root_key=root_key,
        relative_pattern="",
        match_kind=match_kind,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        label=label,
    )


_HF_HUB_CACHE_RULE = _rule(
    "huggingface-hub-cache-vendor-managed",
    root_key="HF_HUB_CACHE",
    label="Hugging Face Hub repository cache; prune with hf cache prune",
    rebuild_cost=RebuildCost.HIGH,
)
_HF_XET_CACHE_RULE = _rule(
    "huggingface-xet-cache-vendor-managed",
    root_key="HF_XET_CACHE",
    label="Hugging Face Xet transfer cache",
    rebuild_cost=RebuildCost.MEDIUM,
)
_HF_ASSETS_CACHE_RULE = _rule(
    "huggingface-assets-cache-vendor-managed",
    root_key="HF_ASSETS_CACHE",
    label="Hugging Face downstream-library assets cache",
    rebuild_cost=RebuildCost.MEDIUM,
)
_HF_HOME_RULE = _rule(
    "huggingface-home-state",
    root_key="HF_HOME",
    label="Hugging Face home persistent state and unknown storage",
    rebuild_cost=RebuildCost.HIGH,
)
_HF_TOKEN_RULE = _rule(
    "huggingface-auth-token",
    root_key="HF_TOKEN_PATH",
    label="Hugging Face authentication token",
    rebuild_cost=RebuildCost.HIGH,
    match_kind=MatchKind.EXACT,
)

HUGGINGFACE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _HF_HUB_CACHE_RULE,
    _HF_XET_CACHE_RULE,
    _HF_ASSETS_CACHE_RULE,
    _HF_HOME_RULE,
    _HF_TOKEN_RULE,
)


def huggingface_roots(
    environment: Mapping[str, str] | None = None,
) -> HuggingFaceRootSet:
    env = _casefold_env(environment)
    userprofile = env.get("userprofile")

    explicit_home = _absolute_path(env.get("hf_home"))
    if explicit_home is not None:
        home = explicit_home
    else:
        xdg = _absolute_path(env.get("xdg_cache_home"))
        if xdg is not None:
            home = xdg / "huggingface"
        elif userprofile:
            home = PureWindowsPath(userprofile) / ".cache" / "huggingface"
        else:
            home = None

    hub = _first_absolute(
        env.get("devclean_hf_hub_cache"),
        env.get("hf_hub_cache"),
        env.get("huggingface_hub_cache"),
    )
    xet = _first_absolute(
        env.get("devclean_hf_xet_cache"),
        env.get("hf_xet_cache"),
    )
    assets = _first_absolute(
        env.get("devclean_hf_assets_cache"),
        env.get("hf_assets_cache"),
        env.get("huggingface_assets_cache"),
    )
    token = _first_absolute(
        env.get("devclean_hf_token_path"),
        env.get("hf_token_path"),
    )

    if home is not None:
        hub = hub or home / "hub"
        xet = xet or home / "xet"
        assets = assets or home / "assets"
        token = token or home / "token"

    return HuggingFaceRootSet(
        home_roots=_tuple_if_path(home),
        hub_cache_roots=_tuple_if_path(hub),
        xet_cache_roots=_tuple_if_path(xet),
        assets_cache_roots=_tuple_if_path(assets),
        token_paths=_tuple_if_path(token),
    )


def huggingface_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = huggingface_roots(environment)
    return tuple(
        dict.fromkeys(
            (
                *roots.hub_cache_roots,
                *roots.xet_cache_roots,
                *roots.assets_cache_roots,
            )
        )
    )


def match_huggingface_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = huggingface_roots(environment)
    matches: list[tuple[int, ApplicationCleanupRule]] = []

    groups = (
        (roots.hub_cache_roots, _HF_HUB_CACHE_RULE, MatchKind.PREFIX),
        (roots.xet_cache_roots, _HF_XET_CACHE_RULE, MatchKind.PREFIX),
        (roots.assets_cache_roots, _HF_ASSETS_CACHE_RULE, MatchKind.PREFIX),
        (roots.token_paths, _HF_TOKEN_RULE, MatchKind.EXACT),
        (roots.home_roots, _HF_HOME_RULE, MatchKind.PREFIX),
    )
    for candidates, rule, match_kind in groups:
        for root in candidates:
            normalized_root = _impl._normalize(root)
            if _impl._matches(normalized, normalized_root, match_kind):
                matches.append((len(normalized_root), rule))

    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def evaluate_huggingface_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_huggingface_rule(path, environment)
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


def huggingface_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    del environment
    return ()


def whole_tree_huggingface_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


@lru_cache(maxsize=1)
def huggingface_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '(?i)^hf\\.exe$' -or "
        "(($_.Name -match '(?i)^python(?:w)?\\.exe$') -and "
        "$_.CommandLine -match '(?i)(?:huggingface|transformers|diffusers|"
        "sentence[_-]transformers)') "
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


def hf_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    override = env.get("devclean_hf_exe")
    if override:
        return override
    return "hf.exe" if os.name == "nt" else "hf"


def clear_huggingface_process_cache() -> None:
    huggingface_process_running.cache_clear()


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


def _tuple_if_path(path: PureWindowsPath | None) -> tuple[PureWindowsPath, ...]:
    return () if path is None else (path,)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "HUGGINGFACE_RULES",
    "HuggingFaceRootSet",
    "clear_huggingface_process_cache",
    "evaluate_huggingface_path",
    "hf_executable",
    "huggingface_audited_tool_roots",
    "huggingface_process_running",
    "huggingface_roots",
    "huggingface_scan_roots",
    "match_huggingface_rule",
    "whole_tree_huggingface_rule",
]
