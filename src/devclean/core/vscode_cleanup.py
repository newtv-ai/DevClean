"""Audited Visual Studio Code storage semantics for Windows cleanup.

VS Code supports Stable/Insiders roots, explicit user-data/extension roots,
portable mode, and Windows-side Remote-WSL server download caches. Regenerable
cache trees are TOOL-owned while workspace/chat/history/recovery state and
installed extensions remain outside generic deletion.
"""

from __future__ import annotations

import os
import re
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
_PORTABLE_TEMP_IDLE_DAYS = 10 / (24 * 60)


@dataclass(frozen=True, slots=True)
class VSCodeRootSet:
    data_roots: tuple[PureWindowsPath, ...]
    extension_roots: tuple[PureWindowsPath, ...]
    temp_roots: tuple[PureWindowsPath, ...]
    wsl_download_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    root_kind: str = "data",
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    user_age_buckets: tuple[int, ...] = (),
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="vscode",
        root_key=f"VSCODE_{root_kind.upper()}",
        relative_pattern=relative_pattern,
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        user_age_buckets=user_age_buckets,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


def _tool_dir(
    rule_id: str,
    relative: str,
    label: str,
    *,
    root_kind: str = "data",
    idle_days: float = 7,
    min_reclaim_bytes: int = 4 * _MIB,
    rebuild_cost: RebuildCost = RebuildCost.LOW,
    size_sensitive_idle: bool = True,
) -> ApplicationCleanupRule:
    return _rule(
        rule_id,
        relative,
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        rebuild_cost,
        label,
        root_kind=root_kind,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=True,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=True,
    )


VSCODE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _tool_dir("vscode-cache", "Cache", "VS Code Chromium resource cache"),
    _tool_dir("vscode-cached-data", "CachedData", "VS Code cached application data"),
    _tool_dir(
        "vscode-cached-configurations",
        "CachedConfigurations",
        "VS Code cached configuration metadata",
    ),
    _tool_dir(
        "vscode-cached-profiles",
        "CachedProfilesData",
        "VS Code cached profile metadata",
    ),
    _tool_dir(
        "vscode-cached-extensions",
        "CachedExtensions",
        "VS Code extension metadata cache",
    ),
    _tool_dir("vscode-code-cache", "Code Cache", "VS Code Chromium code cache"),
    _tool_dir(
        "vscode-gpu-cache",
        "GPUCache",
        "VS Code GPU cache",
        idle_days=3,
    ),
    _tool_dir(
        "vscode-dawn-cache",
        "DawnCache",
        "VS Code WebGPU/Dawn shader cache",
        idle_days=3,
    ),
    _tool_dir(
        "vscode-grshader-cache",
        "GrShaderCache",
        "VS Code graphics shader cache",
        idle_days=3,
    ),
    _tool_dir(
        "vscode-shader-cache",
        "ShaderCache",
        "VS Code graphics shader cache",
        idle_days=3,
    ),
    _tool_dir(
        "vscode-service-worker-cache-storage",
        r"Service Worker\CacheStorage",
        "VS Code service-worker response cache",
        idle_days=7,
    ),
    _tool_dir(
        "vscode-service-worker-script-cache",
        r"Service Worker\ScriptCache",
        "VS Code service-worker script cache",
        idle_days=7,
    ),
    _tool_dir(
        "vscode-extension-vsix-cache",
        "CachedExtensionVSIXs",
        "VS Code downloaded extension package cache",
        idle_days=14,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _tool_dir(
        "vscode-logs",
        "logs",
        "VS Code diagnostic logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "vscode-crashpad-reports",
        r"Crashpad\reports",
        "VS Code Crashpad reports",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "vscode-crashpad-pending",
        r"Crashpad\pending",
        "VS Code pending crash reports",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "vscode-wsl-server-download-cache",
        "",
        "VS Code Remote-WSL downloaded server packages",
        root_kind="wsl_cache",
        idle_days=7,
        min_reclaim_bytes=8 * _MIB,
        rebuild_cost=RebuildCost.MEDIUM,
    ),
    _rule(
        "vscode-workspace-state",
        r"User\workspaceStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "VS Code workspace state and local chat sessions",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "vscode-local-history",
        r"User\History",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "VS Code local file history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "vscode-hot-exit-backups",
        "Backups",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "VS Code unsaved editor / hot-exit recovery data",
    ),
    _rule(
        "vscode-user-state",
        "User",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "VS Code settings, profiles, extension state, snippets, and global storage",
    ),
    _rule(
        "vscode-service-worker-other-state",
        "Service Worker",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified VS Code service-worker persistent state",
    ),
    _rule(
        "vscode-extension-root",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "VS Code installed extensions",
        root_kind="extensions",
    ),
    _rule(
        "vscode-portable-temp",
        "",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "VS Code portable temporary data",
        root_kind="temp",
        idle_days=_PORTABLE_TEMP_IDLE_DAYS,
        requires_process_closed=True,
        size_sensitive_idle=False,
        allow_whole_tree=True,
    ),
    _rule(
        "vscode-unknown-user-data",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified VS Code user-data state",
    ),
)


def vscode_roots(environment: Mapping[str, str] | None = None) -> VSCodeRootSet:
    env = _casefold_env(environment)
    data: list[PureWindowsPath] = []
    extensions: list[PureWindowsPath] = []
    temp: list[PureWindowsPath] = []
    wsl_downloads: list[PureWindowsPath] = []

    appdata = env.get("appdata")
    profile = env.get("userprofile")
    if appdata:
        data.extend(
            (
                PureWindowsPath(appdata) / "Code",
                PureWindowsPath(appdata) / "Code - Insiders",
            )
        )
    if profile:
        profile_path = PureWindowsPath(profile)
        extensions.extend(
            (
                profile_path / ".vscode" / "extensions",
                profile_path / ".vscode-insiders" / "extensions",
            )
        )
        # Remote-WSL downloads server archives/trees to this Windows-side cache
        # before installing them inside a distro. The cache is re-downloadable;
        # remote authoritative state lives inside the distro instead.
        wsl_downloads.append(profile_path / "vscode-remote-wsl" / "stable")

    portable = env.get("vscode_portable")
    if portable:
        portable_root = PureWindowsPath(portable)
        data.insert(0, portable_root / "user-data")
        extensions.insert(0, portable_root / "extensions")
        temp.append(portable_root / "tmp")

    explicit_data = env.get("vscode_user_data_dir")
    explicit_extensions = env.get("vscode_extensions_dir")
    if explicit_data:
        data.insert(0, PureWindowsPath(explicit_data))
    if explicit_extensions:
        extensions.insert(0, PureWindowsPath(explicit_extensions))

    if environment is None:
        running_data, running_extensions = _running_override_roots()
        data[0:0] = running_data
        extensions[0:0] = running_extensions

    return VSCodeRootSet(
        data_roots=_unique_paths(data),
        extension_roots=_unique_paths(extensions),
        temp_roots=_unique_paths(temp),
        wsl_download_roots=_unique_paths(wsl_downloads),
    )


def vscode_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = vscode_roots(environment)
    return (*roots.data_roots, *roots.temp_roots, *roots.wsl_download_roots)


def vscode_storage_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = vscode_roots(environment)
    return (
        *roots.data_roots,
        *roots.extension_roots,
        *roots.temp_roots,
        *roots.wsl_download_roots,
    )


def match_vscode_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = vscode_roots(environment)
    root_groups = {
        "VSCODE_DATA": roots.data_roots,
        "VSCODE_EXTENSIONS": roots.extension_roots,
        "VSCODE_TEMP": roots.temp_roots,
        "VSCODE_WSL_CACHE": roots.wsl_download_roots,
    }
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(VSCODE_RULES):
        for root in root_groups.get(rule.root_key, ()):
            normalized_root = _impl._normalize(root)
            for expanded in _impl._expand_braces(rule.relative_pattern):
                candidate = normalized_root + ("\\" + expanded if expanded else "")
                if _impl._matches(normalized, candidate, rule.match_kind):
                    if rule.owner is DecisionOwner.KEEP:
                        owner_weight = 3
                    elif rule.owner is DecisionOwner.USER:
                        owner_weight = 2
                    else:
                        owner_weight = 1
                    matches.append((len(candidate), owner_weight * 1000 - index, rule))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def vscode_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    roots = vscode_roots(environment)
    root_groups = {
        "VSCODE_DATA": roots.data_roots,
        "VSCODE_TEMP": roots.temp_roots,
        "VSCODE_WSL_CACHE": roots.wsl_download_roots,
    }
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for rule in VSCODE_RULES:
        if rule.owner is not DecisionOwner.TOOL or not rule.allow_whole_tree:
            continue
        if any(token in rule.relative_pattern for token in ("*", "?", "[", "{")):
            continue
        for root in root_groups.get(rule.root_key, ()):
            path = root / rule.relative_pattern if rule.relative_pattern else root
            key = _impl._normalize(path)
            if key in seen:
                continue
            seen.add(key)
            found.append((path, rule))
    return tuple(found)


def whole_tree_vscode_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in vscode_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_vscode_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_vscode_rule(path, environment)
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
            rule,
            PolicyAction.KEEP_PROTECTED,
            observed,
            idle,
            None,
            0,
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
        running = vscode_process_running()
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
def vscode_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$p=Get-Process -Name Code -ErrorAction SilentlyContinue; if ($p) { 'RUNNING' }",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


@lru_cache(maxsize=1)
def _running_override_roots() -> tuple[tuple[PureWindowsPath, ...], tuple[PureWindowsPath, ...]]:
    if os.name != "nt":
        return (), ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'Code.exe' }; "
        "$p | ForEach-Object { $_.CommandLine }"
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
        return (), ()
    if result.returncode != 0:
        return (), ()
    data: list[PureWindowsPath] = []
    extensions: list[PureWindowsPath] = []
    for line in result.stdout.splitlines():
        user_data = _argument_value(line, "--user-data-dir")
        extension_dir = _argument_value(line, "--extensions-dir")
        if user_data:
            data.append(PureWindowsPath(user_data))
        if extension_dir:
            extensions.append(PureWindowsPath(extension_dir))
    return _unique_paths(data), _unique_paths(extensions)


def _argument_value(command_line: str, flag: str) -> str | None:
    pattern = re.compile(
        rf"(?:^|\s){re.escape(flag)}(?:=|\s+)(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>[^\s]+))",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if match is None:
        return None
    return match.group("quoted") or match.group("bare")


def clear_vscode_process_cache() -> None:
    vscode_process_running.cache_clear()
    _running_override_roots.cache_clear()


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    result: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold().rstrip("\\/")
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "VSCODE_RULES",
    "VSCodeRootSet",
    "clear_vscode_process_cache",
    "evaluate_vscode_path",
    "match_vscode_rule",
    "vscode_audited_tool_roots",
    "vscode_process_running",
    "vscode_roots",
    "vscode_scan_roots",
    "vscode_storage_roots",
    "whole_tree_vscode_rule",
]
