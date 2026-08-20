"""Audited Cursor desktop/CLI storage semantics for Windows cleanup.

Cursor mixes regenerable Electron caches with local-only chat history, agent
transcripts, recovery databases, editor history, installed extensions, and
persistent user state. The generic purger may only receive TOOL-owned paths;
chat/history databases remain USER-owned and are maintained with Cursor's own
storage commands instead of raw file deletion.
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
    ApplicationRoot,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    effective_idle_days,
)

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class CursorRootSet:
    roaming: PureWindowsPath | None
    local: PureWindowsPath | None
    program_data: PureWindowsPath | None
    home: PureWindowsPath | None


def _rule(
    rule_id: str,
    root_key: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    size_sensitive_idle: bool = True,
    user_age_buckets: tuple[int, ...] = (),
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="cursor",
        root_key=root_key,
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
    root_key: str,
    relative: str,
    label: str,
    *,
    idle_days: float = 7,
    min_reclaim_bytes: int = 4 * _MIB,
    rebuild_cost: RebuildCost = RebuildCost.LOW,
) -> ApplicationCleanupRule:
    return _rule(
        rule_id,
        root_key,
        relative,
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        rebuild_cost,
        label,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=True,
        allow_whole_tree=True,
    )


def _cache_rules(root_key: str, prefix: str) -> tuple[ApplicationCleanupRule, ...]:
    return (
        _tool_dir(
            f"cursor-{prefix}-cache",
            root_key,
            "Cache",
            "Cursor Chromium resource cache",
        ),
        _tool_dir(
            f"cursor-{prefix}-cached-data",
            root_key,
            "CachedData",
            "Cursor cached application data",
        ),
        _tool_dir(
            f"cursor-{prefix}-code-cache",
            root_key,
            "Code Cache",
            "Cursor Chromium code cache",
        ),
        _tool_dir(
            f"cursor-{prefix}-gpu-cache",
            root_key,
            "GPUCache",
            "Cursor GPU cache",
            idle_days=3,
        ),
        _tool_dir(
            f"cursor-{prefix}-dawn-cache",
            root_key,
            "DawnCache",
            "Cursor WebGPU Dawn cache",
            idle_days=3,
        ),
        _tool_dir(
            f"cursor-{prefix}-grshader-cache",
            root_key,
            "GrShaderCache",
            "Cursor graphics shader cache",
            idle_days=3,
        ),
        _tool_dir(
            f"cursor-{prefix}-shader-cache",
            root_key,
            "ShaderCache",
            "Cursor graphics shader cache",
            idle_days=3,
        ),
        _tool_dir(
            f"cursor-{prefix}-cached-extensions",
            root_key,
            "CachedExtensions",
            "Cursor extension metadata cache",
        ),
        _tool_dir(
            f"cursor-{prefix}-cached-extension-vsix",
            root_key,
            "CachedExtensionVSIXs",
            "Cursor downloaded extension package cache",
            idle_days=14,
            min_reclaim_bytes=8 * _MIB,
            rebuild_cost=RebuildCost.MEDIUM,
        ),
        _tool_dir(
            f"cursor-{prefix}-crashpad-reports",
            root_key,
            r"Crashpad\reports",
            "Cursor Crashpad reports",
            idle_days=1,
            min_reclaim_bytes=_MIB,
            rebuild_cost=RebuildCost.NONE,
        ),
        _tool_dir(
            f"cursor-{prefix}-crashpad-pending",
            root_key,
            r"Crashpad\pending",
            "Cursor pending crash reports",
            idle_days=1,
            min_reclaim_bytes=_MIB,
            rebuild_cost=RebuildCost.NONE,
        ),
    )


# More-specific TOOL/USER rules intentionally outrank broad KEEP roots. Cursor
# staff explicitly recommends Cache/CachedData/Code Cache/GPUCache and extension
# package caches as disposable troubleshooting state. Chat DBs are different:
# raw deletion can break history loading, so they remain USER-owned even when
# they grow to tens of gigabytes.
CURSOR_RULES: tuple[ApplicationCleanupRule, ...] = (
    *_cache_rules("CURSOR_ROAMING", "roaming"),
    *_cache_rules("CURSOR_LOCAL", "local"),
    _tool_dir(
        "cursor-roaming-logs",
        "CURSOR_ROAMING",
        "logs",
        "Cursor diagnostic logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _tool_dir(
        "cursor-local-logs",
        "CURSOR_LOCAL",
        "logs",
        "Cursor local diagnostic logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        rebuild_cost=RebuildCost.NONE,
    ),
    _rule(
        "cursor-workspace-state",
        "CURSOR_ROAMING",
        r"User\workspaceStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor workspace state and local chat metadata/history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-system-workspace-state",
        "CURSOR_PROGRAMDATA",
        r"User\workspaceStorage",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor system-install workspace state and local chat metadata/history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-global-chat-database",
        "CURSOR_ROAMING",
        r"User\globalStorage\{state.vscdb,state.vscdb-wal,state.vscdb-shm}",
        MatchKind.GLOB,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor live chat/agent database; maintain through Cursor commands",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-global-chat-backup",
        "CURSOR_ROAMING",
        r"User\globalStorage\state.vscdb.backup",
        MatchKind.EXACT,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor chat database recovery backup",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-global-chat-recovery-files",
        "CURSOR_ROAMING",
        r"User\globalStorage\state.vscdb.{corrupted.*,broken*,bak*,manual-backup*}",
        MatchKind.GLOB,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor chat database recovery copies; may contain recoverable history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-system-global-chat-database",
        "CURSOR_PROGRAMDATA",
        r"User\globalStorage\{state.vscdb,state.vscdb-wal,state.vscdb-shm,state.vscdb.backup}",
        MatchKind.GLOB,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor system-install local chat/agent database",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-local-history",
        "CURSOR_ROAMING",
        r"User\History",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor local file undo/history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-commit-checkpoints",
        "CURSOR_ROAMING",
        r"User\globalStorage\anysphere.cursor-commits\checkpoints",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor AI edit checkpoints and local undo history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-retrieval-checkpoints",
        "CURSOR_ROAMING",
        r"User\globalStorage\anysphere.cursor-retrieval\checkpoints",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor retrieval/edit checkpoints and local undo history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-agent-projects",
        "CURSOR_HOME",
        "projects",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor local Agent transcripts and project assets",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-cli-chats",
        "CURSOR_HOME",
        "chats",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        RebuildCost.HIGH,
        "Cursor CLI chats stored only on this machine",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "cursor-hot-exit-backups",
        "CURSOR_ROAMING",
        "Backups",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Cursor unsaved editor/recovery data",
    ),
    _rule(
        "cursor-installed-extensions",
        "CURSOR_HOME",
        "extensions",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Cursor installed extensions",
    ),
    _rule(
        "cursor-roaming-user-state",
        "CURSOR_ROAMING",
        "User",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Cursor settings, extension state, snippets, and persistent user state",
    ),
    _rule(
        "cursor-system-user-state",
        "CURSOR_PROGRAMDATA",
        "User",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Cursor system-install persistent user state",
    ),
    _rule(
        "cursor-home-state",
        "CURSOR_HOME",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Cursor home configuration and persistent local data",
    ),
    _rule(
        "cursor-roaming-unknown-state",
        "CURSOR_ROAMING",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Cursor roaming state",
    ),
    _rule(
        "cursor-local-unknown-state",
        "CURSOR_LOCAL",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Cursor local state",
    ),
    _rule(
        "cursor-system-unknown-state",
        "CURSOR_PROGRAMDATA",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Unclassified Cursor system-install state",
    ),
)


def cursor_roots(environment: Mapping[str, str] | None = None) -> CursorRootSet:
    env = _casefold_env(environment)
    appdata = env.get("appdata")
    localappdata = env.get("localappdata")
    programdata = env.get("programdata")
    userprofile = env.get("userprofile")
    return CursorRootSet(
        roaming=PureWindowsPath(appdata) / "Cursor" if appdata else None,
        local=PureWindowsPath(localappdata) / "Cursor" if localappdata else None,
        program_data=PureWindowsPath(programdata) / "Cursor" if programdata else None,
        home=PureWindowsPath(userprofile) / ".cursor" if userprofile else None,
    )


def cursor_application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    roots = cursor_roots(environment)
    pairs = (
        ("CURSOR_ROAMING", roots.roaming),
        ("CURSOR_LOCAL", roots.local),
        ("CURSOR_PROGRAMDATA", roots.program_data),
        ("CURSOR_HOME", roots.home),
    )
    return tuple(ApplicationRoot(key, path) for key, path in pairs if path is not None)


def cursor_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = cursor_roots(environment)
    return tuple(
        path
        for path in (roots.roaming, roots.local, roots.program_data, roots.home)
        if path is not None
    )


def match_cursor_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = {
        root.key: _impl._normalize(root.path) for root in cursor_application_roots(environment)
    }
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(CURSOR_RULES):
        root = roots.get(rule.root_key)
        if root is None:
            continue
        for expanded in _impl._expand_braces(rule.relative_pattern):
            candidate = root + ("\\" + expanded if expanded else "")
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


def evaluate_cursor_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_cursor_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)

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
        running = cursor_process_running()
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
def cursor_process_running() -> bool:
    if os.name != "nt":
        return False
    script = "$p=Get-Process -Name Cursor -ErrorAction SilentlyContinue; if ($p) { 'RUNNING' }"
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
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


def clear_cursor_process_cache() -> None:
    cursor_process_running.cache_clear()


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "CURSOR_RULES",
    "CursorRootSet",
    "clear_cursor_process_cache",
    "cursor_application_roots",
    "cursor_process_running",
    "cursor_roots",
    "cursor_scan_roots",
    "evaluate_cursor_path",
    "match_cursor_rule",
]
