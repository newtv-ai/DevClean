"""Audited Claude Code storage semantics for Windows cleanup.

Claude Code mixes regenerable application data, user history, installed plugins,
and persistent memory below the same configuration root. This module keeps
those meanings explicit and reuses DevClean's application-policy scoring model.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PureWindowsPath

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
_HISTORY_TAIL_BYTES = 128 * 1024
_TEMP_IDLE_DAYS = 10 / (24 * 60)
_SESSION_LOCK_IDLE_DAYS = 10 / (24 * 60)


@dataclass(frozen=True, slots=True)
class ClaudeRootSet:
    config: PureWindowsPath | None
    fallback_config: PureWindowsPath | None
    temp: PureWindowsPath | None
    plugins: PureWindowsPath | None
    profile: PureWindowsPath | None


def _rule(
    rule_id: str,
    root_key: str,
    relative_pattern: str,
    match_kind: MatchKind,
    owner: DecisionOwner,
    last_use: LastUseStrategy,
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
        app_id="claude",
        root_key=root_key,
        relative_pattern=relative_pattern,
        match_kind=match_kind,
        owner=owner,
        last_use=last_use,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        user_age_buckets=user_age_buckets,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


# TOOL paths below are either explicitly documented by Anthropic as losing
# nothing user-facing when removed, or are transient/cache/lock state that is
# recreated. USER paths preserve unique history or reports. KEEP paths contain
# authored configuration, authentication, installed plugins, or persistent
# application/agent state.
CLAUDE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _rule(
        "claude-debug",
        "CLAUDE_HOME",
        "debug",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code debug logs",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-plans",
        "CLAUDE_HOME",
        "plans",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code plan-mode files",
        idle_days=30,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-image-cache",
        "CLAUDE_HOME",
        "image-cache",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Claude Code image attachment cache",
        idle_days=14,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-session-env",
        "CLAUDE_HOME",
        "session-env",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code session environment metadata",
        idle_days=7,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-task-state",
        "CLAUDE_HOME",
        "tasks",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code task state",
        idle_days=7,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-shell-snapshots",
        "CLAUDE_HOME",
        "shell-snapshots",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code leftover shell snapshots",
        idle_days=1,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-session-locks",
        "CLAUDE_HOME",
        "sessions",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code running-session lock metadata",
        idle_days=_SESSION_LOCK_IDLE_DAYS,
        requires_process_closed=True,
        size_sensitive_idle=False,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-config-backups",
        "CLAUDE_HOME",
        "backups",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code migration backups",
        idle_days=30,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-legacy-todos",
        "CLAUDE_HOME",
        "todos",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code legacy todo state",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-legacy-statsig",
        "CLAUDE_HOME",
        "statsig",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code legacy Statsig cache",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-legacy-logs",
        "CLAUDE_HOME",
        "logs",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code legacy logs",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-cache",
        "CLAUDE_HOME",
        "cache",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Claude Code regenerable cache",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "claude-remote-settings-cache",
        "CLAUDE_HOME",
        "remote-settings.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Claude Code server-managed settings cache",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "claude-policy-limits-cache",
        "CLAUDE_HOME",
        "policy-limits.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Claude Code feature policy cache",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "claude-daemon-log",
        "CLAUDE_HOME",
        "daemon.log",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code agent-view supervisor log",
        idle_days=1,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "claude-temp-scratch",
        "CLAUDE_TEMP",
        "",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Claude Code temporary task and scratch output",
        idle_days=_TEMP_IDLE_DAYS,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "claude-project-auto-memory",
        "CLAUDE_HOME",
        r"{projects\*\memory,projects\*\memory\*}",
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code project auto memory",
    ),
    _rule(
        "claude-project-history",
        "CLAUDE_HOME",
        "projects",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.SESSION_LAST_EVENT,
        RebuildCost.HIGH,
        "Claude Code conversation transcripts",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-file-history",
        "CLAUDE_HOME",
        "file-history",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code checkpoint file history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-paste-cache",
        "CLAUDE_HOME",
        "paste-cache",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code pasted content referenced by prompt history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-prompt-history",
        "CLAUDE_HOME",
        "history.jsonl",
        MatchKind.EXACT,
        DecisionOwner.USER,
        LastUseStrategy.JSONL_RECORD_TS,
        RebuildCost.HIGH,
        "Claude Code prompt history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-usage-stats",
        "CLAUDE_HOME",
        "stats-cache.json",
        MatchKind.EXACT,
        DecisionOwner.USER,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code historical usage totals",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-usage-data",
        "CLAUDE_HOME",
        "usage-data",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code insights reports and analysis data",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-feedback-bundles",
        "CLAUDE_HOME",
        "feedback-bundles",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code unsent feedback and bug-report archives",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "claude-settings",
        "CLAUDE_HOME",
        "settings.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code user settings",
    ),
    _rule(
        "claude-credentials",
        "CLAUDE_HOME",
        ".credentials.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code credentials",
    ),
    _rule(
        "claude-global-instructions",
        "CLAUDE_HOME",
        "CLAUDE.md",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code global instructions",
    ),
    _rule(
        "claude-authored-config",
        "CLAUDE_HOME",
        r"{rules,skills,commands,agents,workflows,output-styles,agent-memory,keybindings.json,themes,scheduled-tasks}",
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code authored configuration, scheduled tasks, and persistent agent memory",
    ),
    _rule(
        "claude-home-plugins",
        "CLAUDE_HOME",
        "plugins",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code installed plugins and plugin data",
    ),
    _rule(
        "claude-agent-view-state",
        "CLAUDE_HOME",
        r"{daemon,daemon\*,jobs,jobs\*}",
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code background-agent supervisor and job state",
    ),
    _rule(
        "claude-plugins",
        "CLAUDE_PLUGINS",
        "",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code installed plugins and plugin data",
    ),
    _rule(
        "claude-global-state",
        "CLAUDE_PROFILE",
        ".claude.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.HIGH,
        "Claude Code global authentication and application state",
    ),
)

_CLAUDE_CWD_TEMP_RULE = _rule(
    "claude-cwd-temp-marker",
    "ANYWHERE",
    "tmpclaude-*-cwd",
    MatchKind.GLOB,
    DecisionOwner.TOOL,
    LastUseStrategy.FILE_MTIME,
    RebuildCost.NONE,
    "Claude Code leaked working-directory marker",
    idle_days=1,
    requires_process_closed=True,
    size_sensitive_idle=False,
)


def claude_roots(environment: Mapping[str, str] | None = None) -> ClaudeRootSet:
    env = _casefold_env(environment)
    profile_value = env.get("userprofile")
    profile = PureWindowsPath(profile_value) if profile_value else None
    default_config = profile / ".claude" if profile is not None else None
    configured = env.get("claude_config_dir")
    config = PureWindowsPath(configured) if configured else default_config
    fallback_config = (
        default_config
        if default_config is not None and config is not None and default_config != config
        else None
    )

    temp_base = env.get("claude_code_tmpdir") or env.get("temp") or env.get("tmp")
    temp = PureWindowsPath(temp_base) / "claude" if temp_base else None

    plugin_override = env.get("claude_code_plugin_cache_dir")
    plugins = PureWindowsPath(plugin_override) if plugin_override else None
    if plugins is None and config is not None:
        plugins = config / "plugins"
    return ClaudeRootSet(
        config=config,
        fallback_config=fallback_config,
        temp=temp,
        plugins=plugins,
        profile=profile,
    )


def claude_application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    roots = claude_roots(environment)
    pairs = (
        ("CLAUDE_HOME", roots.config),
        ("CLAUDE_TEMP", roots.temp),
        ("CLAUDE_PLUGINS", roots.plugins),
        ("CLAUDE_PROFILE", roots.profile),
    )
    return tuple(ApplicationRoot(key, path) for key, path in pairs if path is not None)


def claude_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return storage roots worth actively scanning for reclaimable Claude data."""

    roots = claude_roots(environment)
    paths = (roots.config, roots.fallback_config, roots.temp)
    return tuple(dict.fromkeys(path for path in paths if path is not None))


def match_claude_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    custom_memory = _custom_auto_memory_root(environment)
    if custom_memory is not None:
        protected = _impl._normalize(custom_memory)
        if normalized == protected or normalized.startswith(protected.rstrip("\\") + "\\"):
            return _rule(
                "claude-custom-auto-memory",
                "CLAUDE_CUSTOM_MEMORY",
                "",
                MatchKind.PREFIX,
                DecisionOwner.KEEP,
                LastUseStrategy.FILE_MTIME,
                RebuildCost.HIGH,
                "Claude Code custom auto memory",
            )

    filename = PureWindowsPath(os.fspath(path)).name.casefold()
    if fnmatch.fnmatchcase(filename, _CLAUDE_CWD_TEMP_RULE.relative_pattern.casefold()):
        return _CLAUDE_CWD_TEMP_RULE

    resolved = claude_roots(environment)
    application_roots = {
        root.key: (_impl._normalize(root.path),)
        for root in claude_application_roots(environment)
    }
    claude_homes = tuple(
        _impl._normalize(root)
        for root in (resolved.config, resolved.fallback_config)
        if root is not None
    )
    if claude_homes:
        application_roots["CLAUDE_HOME"] = tuple(dict.fromkeys(claude_homes))

    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(CLAUDE_RULES):
        roots = application_roots.get(rule.root_key, ())
        for root in roots:
            for expanded in _impl._expand_braces(rule.relative_pattern):
                candidate = root + ("\\" + expanded if expanded else "")
                if _impl._matches(normalized, candidate, rule.match_kind):
                    owner_weight = 2 if rule.owner is DecisionOwner.KEEP else 1
                    matches.append((len(candidate), owner_weight * 1000 - index, rule))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def evaluate_claude_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_claude_rule(path, environment)
    if rule is None:
        return None
    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(_resolve_last_used(path, rule, last_used))
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
        running = claude_process_running()
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


def _resolve_last_used(
    path: str | os.PathLike[str],
    rule: ApplicationCleanupRule,
    fallback: datetime | None,
) -> datetime | None:
    if rule.last_use is LastUseStrategy.JSONL_RECORD_TS:
        return _latest_jsonl_timestamp(Path(path)) or fallback
    return fallback


def _latest_jsonl_timestamp(path: Path) -> datetime | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _HISTORY_TAIL_BYTES), os.SEEK_SET)
            data = handle.read()
    except OSError:
        return None
    lines = data.splitlines()
    if size > _HISTORY_TAIL_BYTES and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        timestamp = payload.get("timestamp", payload.get("ts"))
        if isinstance(timestamp, str):
            try:
                return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(timestamp, (int, float)):
            try:
                return datetime.fromtimestamp(float(timestamp), tz=UTC)
            except (OSError, OverflowError, ValueError):
                continue
    return None


def _custom_auto_memory_root(
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    roots = claude_roots(environment)
    if roots.config is None:
        return None
    return _custom_auto_memory_root_cached(
        str(roots.config),
        None if roots.profile is None else str(roots.profile),
    )


@lru_cache(maxsize=16)
def _custom_auto_memory_root_cached(
    config_text: str,
    profile_text: str | None,
) -> PureWindowsPath | None:
    config = PureWindowsPath(config_text)
    profile = PureWindowsPath(profile_text) if profile_text else None
    settings = Path(str(config / "settings.json"))
    try:
        payload = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("autoMemoryDirectory")
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("~/") or value.startswith("~\\"):
        if profile is None:
            return None
        return profile / value[2:]
    candidate = PureWindowsPath(value)
    return candidate if candidate.is_absolute() else None


@lru_cache(maxsize=1)
def claude_process_running() -> bool:
    """Detect native Claude and legacy npm Claude Code processes on Windows."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'claude.exe' -or "
        "(($_.Name -ieq 'node.exe' -or $_.Name -ieq 'bun.exe') -and "
        "$_.CommandLine -match '(?i)(@anthropic-ai[\\\\/]claude-code|claude-code)') "
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
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


def clear_claude_process_cache() -> None:
    claude_process_running.cache_clear()
    _custom_auto_memory_root_cached.cache_clear()


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "CLAUDE_RULES",
    "ClaudeRootSet",
    "claude_application_roots",
    "claude_process_running",
    "claude_roots",
    "claude_scan_roots",
    "clear_claude_process_cache",
    "evaluate_claude_path",
    "match_claude_rule",
]
