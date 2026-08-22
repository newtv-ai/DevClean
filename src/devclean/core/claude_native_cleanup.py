"""Windows-native Claude Code updater leftovers integrated into normal scanning.

Anthropic's native Windows installer keeps per-version binaries below
``~/.local/share/claude/versions`` and activates one by copying it to
``~/.local/bin/claude.exe``.  Those files are updater payload, not conversation
or configuration state.  Keep the newest valid staged binary as a recovery
copy, classify older valid staged binaries as deterministic cleanup candidates,
and remove ``claude.exe.old.*`` only when the active launcher can be verified.

This module is installed very early from ``devclean.core`` so the existing
application cleanup facade imports the augmented Claude functions.  It is kept
separate from the much larger source-audited Claude state model so native
installer policy can be reviewed independently.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core import claude_cleanup as _claude
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    ApplicationRoot,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)

_MIN_VALID_NATIVE_BINARY_BYTES = 8 * 1024 * 1024
_NATIVE_VERSION_RE = re.compile(
    r"^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)
_NATIVE_OLD_LAUNCHER_RE = re.compile(r"^claude\.exe\.old(?:\.\d+)?$", re.IGNORECASE)


def _rule(
    rule_id: str,
    root_key: str,
    owner: DecisionOwner,
    label: str,
    *,
    requires_process_closed: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="claude",
        root_key=root_key,
        relative_pattern="",
        match_kind=MatchKind.EXACT,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.NONE,
        idle_days=0 if owner is DecisionOwner.TOOL else None,
        min_reclaim_bytes=0,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=False,
        user_age_buckets=(),
        allow_whole_tree=False,
        label=label,
    )


_NATIVE_STALE_VERSION = _rule(
    "claude-native-stale-version",
    "CLAUDE_NATIVE_VERSIONS",
    DecisionOwner.TOOL,
    "Claude Code 原生安装器旧版本二进制",
    requires_process_closed=True,
)
_NATIVE_PRESERVED_VERSION = _rule(
    "claude-native-preserved-version",
    "CLAUDE_NATIVE_VERSIONS",
    DecisionOwner.KEEP,
    "Claude Code 原生安装器最新恢复版本",
)
_NATIVE_OLD_LAUNCHER = _rule(
    "claude-native-old-launcher",
    "CLAUDE_NATIVE_BIN",
    DecisionOwner.TOOL,
    "Claude Code 更新后遗留的旧启动器",
    requires_process_closed=True,
)
_NATIVE_LAUNCHER = _rule(
    "claude-native-launcher",
    "CLAUDE_NATIVE_BIN",
    DecisionOwner.KEEP,
    "Claude Code 当前原生启动器",
)
_NATIVE_UNVERIFIED_BACKUP = _rule(
    "claude-native-unverified-backup",
    "CLAUDE_NATIVE_BIN",
    DecisionOwner.KEEP,
    "Claude Code 启动器无法验证时保留更新备份",
)

_ORIGINAL_APPLICATION_ROOTS = _claude.claude_application_roots
_ORIGINAL_SCAN_ROOTS = _claude.claude_scan_roots
_ORIGINAL_MATCH_RULE = _claude.match_claude_rule
_ORIGINAL_CLEAR_CACHE = _claude.clear_claude_process_cache


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


def _native_paths(
    environment: Mapping[str, str] | None,
) -> tuple[PureWindowsPath | None, PureWindowsPath | None]:
    profile = _casefold_env(environment).get("userprofile")
    if not profile:
        return None, None
    home = PureWindowsPath(profile)
    return home / ".local" / "share" / "claude" / "versions", home / ".local" / "bin"


def claude_application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    roots = list(_ORIGINAL_APPLICATION_ROOTS(environment))
    versions, binary_dir = _native_paths(environment)
    if versions is not None:
        roots.append(ApplicationRoot("CLAUDE_NATIVE_VERSIONS", versions))
    if binary_dir is not None:
        roots.append(ApplicationRoot("CLAUDE_NATIVE_BIN", binary_dir))
    return tuple(dict.fromkeys(roots))


def claude_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = list(_ORIGINAL_SCAN_ROOTS(environment))
    versions, binary_dir = _native_paths(environment)
    if versions is not None:
        roots.append(versions)
    if binary_dir is not None:
        # ``~/.local/bin`` is normally tiny.  It is included only so exact
        # ``claude.exe.old.*`` updater backups can be discovered without a
        # three-hour traversal of the entire user profile.
        roots.append(binary_dir)
    return tuple(dict.fromkeys(roots))


def _direct_child_name(path: str, parent: PureWindowsPath | None) -> str | None:
    if parent is None:
        return None
    normalized = _impl._normalize(path)
    root = _impl._normalize(parent)
    prefix = root.rstrip("\\") + "\\"
    if not normalized.startswith(prefix):
        return None
    tail = normalized[len(prefix) :]
    if not tail or "\\" in tail:
        return None
    return PureWindowsPath(path).name


def _version_key(name: str) -> tuple[tuple[int, ...], int, str]:
    core = re.split(r"[-+]", name, maxsplit=1)[0]
    numbers = tuple(int(part) for part in core.split("."))
    # Stable releases sort after prerelease/build-suffixed variants that share
    # the same numeric core.  The final text component makes ordering total and
    # deterministic without pretending to implement every semver nuance.
    stable = int(core == name)
    return numbers, stable, name.casefold()


@lru_cache(maxsize=16)
def _newest_valid_staged_version(directory: str) -> str | None:
    root = Path(directory)
    candidates: list[str] = []
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not _NATIVE_VERSION_RE.fullmatch(entry.name):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if not entry.is_file() or stat.st_size < _MIN_VALID_NATIVE_BINARY_BYTES:
            continue
        candidates.append(entry.name)
    return max(candidates, key=_version_key) if candidates else None


@lru_cache(maxsize=16)
def _healthy_launcher(path: str) -> bool:
    launcher = Path(path)
    try:
        if not launcher.is_file() or launcher.stat().st_size < _MIN_VALID_NATIVE_BINARY_BYTES:
            return False
        result = subprocess.run(
            [str(launcher), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    output = f"{result.stdout}\n{result.stderr}".casefold()
    return result.returncode == 0 and "claude code" in output


def match_claude_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    rendered = os.fspath(path)
    versions, binary_dir = _native_paths(environment)

    version_name = _direct_child_name(rendered, versions)
    if version_name is not None and _NATIVE_VERSION_RE.fullmatch(version_name):
        if versions is None:
            return _NATIVE_PRESERVED_VERSION
        newest = _newest_valid_staged_version(str(versions))
        # Fail closed for a partial/corrupt download and always retain one valid
        # newest recovery payload.  Every older valid native payload is updater
        # residue and can be regenerated by ``claude update``/reinstall.
        try:
            candidate_size = Path(rendered).stat().st_size
        except OSError:
            return _NATIVE_PRESERVED_VERSION
        if candidate_size < _MIN_VALID_NATIVE_BINARY_BYTES:
            return _NATIVE_PRESERVED_VERSION
        if newest is None or version_name.casefold() == newest.casefold():
            return _NATIVE_PRESERVED_VERSION
        return _NATIVE_STALE_VERSION

    binary_name = _direct_child_name(rendered, binary_dir)
    if binary_name is not None:
        if binary_name.casefold() in {"claude", "claude.exe"}:
            return _NATIVE_LAUNCHER
        if _NATIVE_OLD_LAUNCHER_RE.fullmatch(binary_name):
            if binary_dir is None:
                return _NATIVE_UNVERIFIED_BACKUP
            launcher = binary_dir / "claude.exe"
            return _NATIVE_OLD_LAUNCHER if _healthy_launcher(str(launcher)) else _NATIVE_UNVERIFIED_BACKUP

    return _ORIGINAL_MATCH_RULE(path, environment)


def clear_claude_process_cache() -> None:
    _newest_valid_staged_version.cache_clear()
    _healthy_launcher.cache_clear()
    _ORIGINAL_CLEAR_CACHE()


def install() -> None:
    """Install native updater rules before the facade snapshots these callables."""

    if getattr(_claude, "_devclean_native_updater_rules", False):
        return
    _claude.claude_application_roots = claude_application_roots
    _claude.claude_scan_roots = claude_scan_roots
    _claude.match_claude_rule = match_claude_rule
    _claude.clear_claude_process_cache = clear_claude_process_cache
    setattr(_claude, "_devclean_native_updater_rules", True)


install()


__all__ = [
    "claude_application_roots",
    "claude_scan_roots",
    "clear_claude_process_cache",
    "install",
    "match_claude_rule",
]
