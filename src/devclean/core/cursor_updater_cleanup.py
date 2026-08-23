"""Source-backed cleanup for superseded Cursor Windows updater staging.

Cursor's Windows Inno updater stages installers below temporary directories named
``vscode-stable-user-x64-*`` or ``vscode-stable-system-x64-*``. Cursor support
has explicitly documented these directories as updater payloads and, for a
long-running updater leak, stated that the accumulated directories are safe to
delete.

One nuance matters for a cleaner: the newest downloaded installer can be useful
as a local recovery path after a failed update. DevClean therefore keeps one
highest-version recovery directory per user/system family and treats only older
or duplicate staging directories as deterministic whole-tree cleanup. Matching
partial staging directories with no installer are abandoned updater scratch once
Cursor and its updater are no longer running.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core import application_cleanup as _application
from devclean.core import cursor_cleanup as _cursor
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)

_REPARSE_POINT_ATTRIBUTE = 0x400
_STAGING_RE = re.compile(
    r"^vscode-stable-(?P<family>user|system)-x64-[0-9a-z]+$",
    re.IGNORECASE,
)
_INSTALLER_RE = re.compile(
    r"^CodeSetup-stable-(?P<version>\d+(?:\.\d+)+)\.exe$",
    re.IGNORECASE,
)

_TOOL_RULE = ApplicationCleanupRule(
    rule_id="cursor-superseded-windows-update-staging",
    app_id="cursor",
    root_key="CURSOR_UPDATE_TEMP",
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
    label="Cursor 已被后续版本取代的 Windows 更新暂存目录",
)

_RECOVERY_RULE = ApplicationCleanupRule(
    rule_id="cursor-latest-windows-update-recovery",
    app_id="cursor",
    root_key="CURSOR_UPDATE_TEMP",
    relative_pattern="",
    match_kind=MatchKind.PREFIX,
    owner=DecisionOwner.KEEP,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.MEDIUM,
    idle_days=None,
    min_reclaim_bytes=0,
    requires_process_closed=False,
    size_sensitive_idle=False,
    user_age_buckets=(),
    allow_whole_tree=False,
    label="Cursor 最新可恢复的 Windows 更新安装包",
)

_ORIGINAL_MATCH_CURSOR_RULE = _cursor.match_cursor_rule
_ORIGINAL_CURSOR_PROCESS_RUNNING = _cursor.cursor_process_running
_ORIGINAL_AUDITED_DYNAMIC_TOOL_ROOTS = _application.audited_dynamic_tool_roots


@dataclass(frozen=True, slots=True)
class _StagingEntry:
    path: PureWindowsPath
    family: str
    version: tuple[int, ...] | None
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _StagingInventory:
    tool_roots: tuple[PureWindowsPath, ...]
    recovery_roots: tuple[PureWindowsPath, ...]


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


def _temp_roots(environment: Mapping[str, str] | None) -> tuple[Path, ...]:
    env = _casefold_env(environment)
    candidates: list[Path] = []
    for key in ("temp", "tmp"):
        value = env.get(key)
        if value:
            candidates.append(Path(value))
    local = env.get("localappdata")
    if local:
        candidates.append(Path(local) / "Temp")

    unique: dict[str, Path] = {}
    for path in candidates:
        unique.setdefault(_impl._normalize(path), path)
    return tuple(unique.values())


def _is_plain_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    return not bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _is_plain_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return not bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _installer_version(directory: Path) -> tuple[int, ...] | None:
    versions: list[tuple[int, ...]] = []
    try:
        children = tuple(directory.iterdir())
    except OSError:
        return None
    for child in children:
        match = _INSTALLER_RE.fullmatch(child.name)
        if match is None or not _is_plain_file(child):
            continue
        versions.append(tuple(int(part) for part in match.group("version").split(".")))
    return max(versions, default=None)


def _inventory(environment: Mapping[str, str] | None) -> _StagingInventory:
    # Do not cache this inventory. Scan-time classification may race with an
    # updater that is still writing the newest recovery installer, and the
    # execution-time whole-tree policy must be able to observe that change and
    # revoke a previously-safe candidate before mutation.
    entries: list[_StagingEntry] = []
    for root in _temp_roots(environment):
        try:
            children = tuple(root.iterdir())
        except OSError:
            continue
        for child in children:
            match = _STAGING_RE.fullmatch(child.name)
            if match is None or not _is_plain_directory(child):
                continue
            try:
                mtime_ns = child.stat().st_mtime_ns
            except OSError:
                continue
            entries.append(
                _StagingEntry(
                    PureWindowsPath(str(child)),
                    match.group("family").casefold(),
                    _installer_version(child),
                    mtime_ns,
                )
            )

    grouped: dict[tuple[str, str], list[_StagingEntry]] = defaultdict(list)
    for entry in entries:
        parent = _impl._normalize(entry.path.parent)
        grouped[(parent, entry.family)].append(entry)

    tool: list[PureWindowsPath] = []
    recovery: list[PureWindowsPath] = []
    for family_entries in grouped.values():
        recoverable = [entry for entry in family_entries if entry.version is not None]
        selected: _StagingEntry | None = None
        if recoverable:
            selected = max(
                recoverable,
                key=lambda entry: (entry.version or (), entry.mtime_ns, str(entry.path).casefold()),
            )
            recovery.append(selected.path)
        for entry in family_entries:
            if selected is not None and entry.path == selected.path:
                continue
            tool.append(entry.path)

    tool.sort(key=lambda path: str(path).casefold())
    recovery.sort(key=lambda path: str(path).casefold())
    return _StagingInventory(tuple(tool), tuple(recovery))


def _top_staging_directory(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    normalized = _impl._normalize(path)
    for root in _temp_roots(environment):
        root_norm = _impl._normalize(root)
        prefix = root_norm.rstrip("\\") + "\\"
        if not normalized.startswith(prefix):
            continue
        tail = normalized[len(prefix) :]
        if not tail:
            continue
        top = tail.split("\\", 1)[0]
        if _STAGING_RE.fullmatch(top) is None:
            continue
        return PureWindowsPath(str(root)) / top
    return None


def match_cursor_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    top = _top_staging_directory(path, environment)
    if top is not None:
        snapshot = _inventory(environment)
        normalized = _impl._normalize(top)
        if any(_impl._normalize(root) == normalized for root in snapshot.tool_roots):
            return _TOOL_RULE
        # Any matching staging name that is not proven superseded stays out of
        # generic TEMP deletion, including the selected recovery installer and
        # malformed/reparse layouts.
        return _RECOVERY_RULE
    return _ORIGINAL_MATCH_CURSOR_RULE(path, environment)


def cursor_update_staging_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    return tuple((path, _TOOL_RULE) for path in _inventory(environment).tool_roots)


@lru_cache(maxsize=1)
def cursor_update_process_running() -> bool:
    _ORIGINAL_CURSOR_PROCESS_RUNNING.cache_clear()
    if _ORIGINAL_CURSOR_PROCESS_RUNNING():
        return True
    if os.name != "nt":
        return False
    script = (
        "$p=Get-Process -ErrorAction SilentlyContinue | Where-Object { "
        "$_.ProcessName -eq 'inno_updater' -or "
        "$_.ProcessName -like 'CodeSetup-stable-*' -or "
        "$_.ProcessName -like 'Cursor*Setup*' }; "
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
    if result.returncode != 0:
        return True
    return "RUNNING" in result.stdout


def install() -> None:
    if getattr(_cursor, "_devclean_cursor_updater_rules", False):
        return

    _cursor.match_cursor_rule = match_cursor_rule
    _cursor.cursor_process_running = cursor_update_process_running
    vars(_cursor)["_devclean_cursor_updater_rules"] = True

    # The shared facade is already loaded by the earlier Claude plugin extension,
    # so update the local callable snapshots it uses for matching, process guards
    # and dynamic whole-tree root discovery. These facade names are deliberately
    # private implementation details, so patch through the module namespace
    # instead of pretending they are public exports for the type checker.
    vars(_application)["match_cursor_rule"] = match_cursor_rule
    vars(_application)["cursor_process_running"] = cursor_update_process_running

    def audited_dynamic_tool_roots(
        environment: Mapping[str, str] | None = None,
    ) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
        return (
            *_ORIGINAL_AUDITED_DYNAMIC_TOOL_ROOTS(environment),
            *cursor_update_staging_audited_tool_roots(environment),
        )

    _application.audited_dynamic_tool_roots = audited_dynamic_tool_roots


install()


__all__ = [
    "cursor_update_process_running",
    "cursor_update_staging_audited_tool_roots",
    "install",
    "match_cursor_rule",
]
