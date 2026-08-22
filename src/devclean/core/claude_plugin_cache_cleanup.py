"""Audited Claude Code plugin staging-cache cleanup.

Claude Code keeps installed plugin versions below ``plugins/cache/<marketplace>/...``
and those versioned directories are live application payload, not generic cache junk.
The plugin manager also creates top-level staging clones such as ``temp_git_*``,
``temp_github_*`` and ``temp_subdir_*.clone`` while installing or refreshing
marketplaces. Public Claude Code bug reports show these staging directories can
leak after successful operations and grow to multiple gigabytes.

This extension grants whole-tree TOOL authority only to a direct child of the
plugin cache when all of the following are true:

* its name matches one of the observed staging formats;
* the embedded immutable epoch-millisecond timestamp is at least three hours old;
* the object is a real directory, not a symlink/reparse point; and
* Claude Code is closed at execution time through the normal application guard.

Installed plugin versions, marketplace state, registry JSON and arbitrary
``temp_*`` names remain protected by the base Claude rules.
"""

from __future__ import annotations

import os
import re
import stat
import time
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core import claude_cleanup as _claude
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)

_STAGING_MIN_AGE_MS = 3 * 60 * 60 * 1000
_REPARSE_POINT_ATTRIBUTE = 0x400
_STAGING_NAME_RE = re.compile(
    r"^(?:"
    r"temp_(?:git|github)_(?P<direct>\d{13})_[0-9A-Za-z_-]+"
    r"|temp_subdir_(?P<subdir>\d{13})_[0-9A-Za-z_-]+\.clone"
    r")$",
    re.IGNORECASE,
)

_STAGING_RULE = ApplicationCleanupRule(
    rule_id="claude-plugin-stale-staging-clone",
    app_id="claude",
    root_key="CLAUDE_PLUGIN_STAGING",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.NONE,
    idle_days=0,
    min_reclaim_bytes=0,
    requires_process_closed=True,
    size_sensitive_idle=False,
    user_age_buckets=(),
    allow_whole_tree=True,
    label="Claude Code 插件安装/更新遗留的暂存克隆",
)

_ORIGINAL_MATCH_CLAUDE_RULE = _claude.match_claude_rule


def _plugin_cache_root(
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    plugins = _claude.claude_roots(environment).plugins
    return None if plugins is None else plugins / "cache"


def _embedded_epoch_ms(name: str) -> int | None:
    match = _STAGING_NAME_RE.fullmatch(name)
    if match is None:
        return None
    value = match.group("direct") or match.group("subdir")
    try:
        return int(value)
    except ValueError:
        return None


def _is_stale_staging_name(name: str, *, now_ms: int) -> bool:
    created_ms = _embedded_epoch_ms(name)
    if created_ms is None or created_ms > now_ms:
        return False
    return now_ms - created_ms >= _STAGING_MIN_AGE_MS


def _is_plain_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    return not bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _staging_root_for_path(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None,
    *,
    now_ms: int,
) -> PureWindowsPath | None:
    cache_root = _plugin_cache_root(environment)
    if cache_root is None:
        return None
    normalized = _impl._normalize(path)
    cache = _impl._normalize(cache_root)
    prefix = cache.rstrip("\\") + "\\"
    if not normalized.startswith(prefix):
        return None
    tail = normalized[len(prefix) :]
    if not tail:
        return None
    top = tail.split("\\", 1)[0]
    if not _is_stale_staging_name(top, now_ms=now_ms):
        return None
    candidate = cache_root / top
    if not _is_plain_directory(Path(str(candidate))):
        return None
    return candidate


def match_claude_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return TOOL authority for stale plugin staging trees only."""

    candidate = _staging_root_for_path(
        path,
        environment,
        now_ms=time.time_ns() // 1_000_000,
    )
    if candidate is not None:
        return _STAGING_RULE
    return _ORIGINAL_MATCH_CLAUDE_RULE(path, environment)


def claude_plugin_staging_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
    *,
    now_ms: int | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return exact stale staging directories as whole-tree cleanup roots."""

    cache_root = _plugin_cache_root(environment)
    if cache_root is None:
        return ()
    current_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    directory = Path(str(cache_root))
    try:
        entries = tuple(directory.iterdir())
    except OSError:
        return ()

    accepted: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    for entry in entries:
        if not _is_stale_staging_name(entry.name, now_ms=current_ms):
            continue
        if not _is_plain_directory(entry):
            continue
        accepted.append((PureWindowsPath(str(entry)), _STAGING_RULE))
    accepted.sort(key=lambda item: str(item[0]).casefold())
    return tuple(accepted)


def install() -> None:
    """Install the matcher before the application facade snapshots Claude rules."""

    if getattr(_claude, "_devclean_plugin_staging_rules", False):
        return
    _claude.match_claude_rule = match_claude_rule
    vars(_claude)["_devclean_plugin_staging_rules"] = True

    # Import the facade only after the Claude matcher is installed so its local
    # ``match_claude_rule`` reference points at this source-specific wrapper.
    from devclean.core import application_cleanup as application_cleanup

    original_dynamic = application_cleanup.audited_dynamic_tool_roots

    def audited_dynamic_tool_roots(
        environment: Mapping[str, str] | None = None,
    ) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
        return (
            *original_dynamic(environment),
            *claude_plugin_staging_audited_tool_roots(environment),
        )

    application_cleanup.audited_dynamic_tool_roots = audited_dynamic_tool_roots


install()


__all__ = [
    "claude_plugin_staging_audited_tool_roots",
    "install",
    "match_claude_rule",
]
