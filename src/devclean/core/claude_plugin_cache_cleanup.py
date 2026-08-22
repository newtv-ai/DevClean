"""Audited Claude Code plugin cache garbage collection.

Claude Code copies marketplace plugins into ``plugins/cache``. Installed version
folders are live application payload and must not be treated as generic cache
junk. Two narrower classes have source-backed disposal semantics:

* leaked top-level install/update staging clones (``temp_git_*``,
  ``temp_github_*`` and ``temp_subdir_*.clone``); and
* version directories carrying Claude's ``.orphaned_at`` marker after the
  documented seven-day grace period, but only when a different sibling version
  is still registered as the active replacement.

The orphan rule deliberately does more than trust the marker. Real Claude Code
bugs have produced transient orphan markers and registry/path drift. DevClean
therefore requires valid plugin registries, proves the candidate is not
referenced by any ``installPath``, protects marketplace ``installLocation``
subtrees, and requires a registered sibling replacement. Unmarked superseded
versions and orphaned versions after a full uninstall remain protected pending a
stronger reference model.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
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
_ORPHAN_GRACE_MS = 7 * 24 * 60 * 60 * 1000
_REPARSE_POINT_ATTRIBUTE = 0x400
_EPOCH_MS_RE = re.compile(r"^\d{13}$")
_STAGING_NAME_RE = re.compile(
    r"^(?:"
    r"temp_(?:git|github)_(?P<direct>\d{13})_[0-9A-Za-z_-]+"
    r"|temp_subdir_(?P<subdir>\d{13})_[0-9A-Za-z_-]+\.clone"
    r")$",
    re.IGNORECASE,
)


def _tool_rule(rule_id: str, root_key: str, label: str) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="claude",
        root_key=root_key,
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
        label=label,
    )


_STAGING_RULE = _tool_rule(
    "claude-plugin-stale-staging-clone",
    "CLAUDE_PLUGIN_STAGING",
    "Claude Code 插件安装/更新遗留的暂存克隆",
)
_ORPHAN_RULE = _tool_rule(
    "claude-plugin-expired-orphan-version",
    "CLAUDE_PLUGIN_ORPHAN_VERSION",
    "Claude Code 已过 7 天宽限期且有活动替代版本的孤立插件版本",
)

_ORIGINAL_MATCH_CLAUDE_RULE = _claude.match_claude_rule
_ORIGINAL_CLEAR_CLAUDE_PROCESS_CACHE = _claude.clear_claude_process_cache


@dataclass(frozen=True, slots=True)
class _PluginReferenceState:
    valid: bool
    install_paths: tuple[str, ...] = ()
    marketplace_paths: tuple[str, ...] = ()


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


def _plugin_root(environment: Mapping[str, str] | None) -> PureWindowsPath | None:
    return _claude.claude_roots(environment).plugins


def _plugin_cache_root(
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    plugins = _plugin_root(environment)
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


def _is_plain_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    return not bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _plain_directory_chain(candidate: Path, cache_root: Path) -> bool:
    """Reject reparse/symlink tricks anywhere inside the cache ownership chain."""

    current = candidate
    boundary = os.path.normcase(os.path.normpath(str(cache_root)))
    while True:
        if not _is_plain_directory(current):
            return False
        rendered = os.path.normcase(os.path.normpath(str(current)))
        if rendered == boundary:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


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
    if not _plain_directory_chain(Path(str(candidate)), Path(str(cache_root))):
        return None
    return candidate


def _version_root_for_path(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None,
) -> PureWindowsPath | None:
    """Resolve ``cache/<marketplace>/<plugin>/<version>`` from a descendant."""

    cache_root = _plugin_cache_root(environment)
    if cache_root is None:
        return None
    normalized = _impl._normalize(path)
    cache = _impl._normalize(cache_root)
    prefix = cache.rstrip("\\") + "\\"
    if not normalized.startswith(prefix):
        return None
    parts = normalized[len(prefix) :].split("\\")
    if len(parts) < 3 or any(not part for part in parts[:3]):
        return None
    return cache_root / parts[0] / parts[1] / parts[2]


def _marker_epoch_ms(candidate: PureWindowsPath, *, now_ms: int) -> int | None:
    marker = Path(str(candidate / ".orphaned_at"))
    if not _is_plain_file(marker):
        return None
    try:
        value = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if _EPOCH_MS_RE.fullmatch(value) is None:
        return None
    try:
        marked_ms = int(value)
    except ValueError:
        return None
    if marked_ms > now_ms or now_ms - marked_ms < _ORPHAN_GRACE_MS:
        return None
    return marked_ms


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _expand_reference_path(value: str, profile: str | None) -> str | None:
    rendered = value.strip()
    if not rendered:
        return None
    if rendered.startswith(("~/", "~\\")):
        if profile is None:
            return None
        rendered = str(PureWindowsPath(profile) / rendered[2:])
    return _impl._normalize(PureWindowsPath(rendered))


@lru_cache(maxsize=32)
def _reference_state_cached(
    plugin_root_text: str,
    profile: str | None,
    installed_signature: tuple[int, int] | None,
    marketplace_signature: tuple[int, int] | None,
) -> _PluginReferenceState:
    """Parse both registries; signatures invalidate normal scan-time caching."""

    del installed_signature, marketplace_signature
    plugin_root = Path(plugin_root_text)
    installed = plugin_root / "installed_plugins.json"
    marketplaces = plugin_root / "known_marketplaces.json"
    if not installed.is_file() or not marketplaces.is_file():
        return _PluginReferenceState(False)
    try:
        installed_payload = json.loads(installed.read_text(encoding="utf-8"))
        marketplace_payload = json.loads(marketplaces.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _PluginReferenceState(False)

    if not isinstance(installed_payload, dict):
        return _PluginReferenceState(False)
    plugins = installed_payload.get("plugins")
    if not isinstance(plugins, dict):
        return _PluginReferenceState(False)

    install_paths: list[str] = []
    for records in plugins.values():
        if not isinstance(records, list):
            return _PluginReferenceState(False)
        for record in records:
            if not isinstance(record, dict):
                return _PluginReferenceState(False)
            value = record.get("installPath")
            if not isinstance(value, str):
                return _PluginReferenceState(False)
            normalized = _expand_reference_path(value, profile)
            if normalized is None:
                return _PluginReferenceState(False)
            install_paths.append(normalized)

    if not isinstance(marketplace_payload, dict):
        return _PluginReferenceState(False)
    marketplace_entries = marketplace_payload.get("marketplaces", marketplace_payload)
    if not isinstance(marketplace_entries, dict):
        return _PluginReferenceState(False)

    marketplace_paths: list[str] = []
    for entry in marketplace_entries.values():
        if not isinstance(entry, dict):
            return _PluginReferenceState(False)
        value = entry.get("installLocation")
        if not isinstance(value, str):
            return _PluginReferenceState(False)
        normalized = _expand_reference_path(value, profile)
        if normalized is None:
            return _PluginReferenceState(False)
        marketplace_paths.append(normalized)

    return _PluginReferenceState(
        True,
        tuple(dict.fromkeys(install_paths)),
        tuple(dict.fromkeys(marketplace_paths)),
    )


def _reference_state(
    environment: Mapping[str, str] | None,
) -> _PluginReferenceState:
    plugin_root = _plugin_root(environment)
    if plugin_root is None:
        return _PluginReferenceState(False)
    root = Path(str(plugin_root))
    env = _casefold_env(environment)
    profile = env.get("userprofile")
    return _reference_state_cached(
        str(root),
        profile,
        _file_signature(root / "installed_plugins.json"),
        _file_signature(root / "known_marketplaces.json"),
    )


def _paths_overlap(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.startswith(right.rstrip("\\") + "\\") or right.startswith(
        left.rstrip("\\") + "\\"
    )


def _has_registered_sibling(candidate: PureWindowsPath, install_paths: tuple[str, ...]) -> bool:
    candidate_normalized = _impl._normalize(candidate)
    parent = _impl._normalize(candidate.parent)
    for install_path in install_paths:
        if install_path == candidate_normalized:
            continue
        if _impl._normalize(PureWindowsPath(install_path).parent) == parent:
            return True
    return False


def _is_expired_unreferenced_orphan(
    candidate: PureWindowsPath,
    environment: Mapping[str, str] | None,
    *,
    now_ms: int,
) -> bool:
    cache_root = _plugin_cache_root(environment)
    if cache_root is None:
        return False
    if _marker_epoch_ms(candidate, now_ms=now_ms) is None:
        return False
    if not _plain_directory_chain(Path(str(candidate)), Path(str(cache_root))):
        return False

    references = _reference_state(environment)
    if not references.valid:
        return False
    candidate_normalized = _impl._normalize(candidate)
    if any(_paths_overlap(candidate_normalized, path) for path in references.install_paths):
        return False
    if any(_paths_overlap(candidate_normalized, path) for path in references.marketplace_paths):
        return False
    # Stronger than the marker alone: prove update/replacement, not merely
    # absence from a registry that is known to drift or occasionally be lost.
    return _has_registered_sibling(candidate, references.install_paths)


def _orphan_root_for_path(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None,
    *,
    now_ms: int,
) -> PureWindowsPath | None:
    candidate = _version_root_for_path(path, environment)
    if candidate is None:
        return None
    return (
        candidate
        if _is_expired_unreferenced_orphan(candidate, environment, now_ms=now_ms)
        else None
    )


def match_claude_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return TOOL authority only for source-proven plugin garbage."""

    current_ms = time.time_ns() // 1_000_000
    staging = _staging_root_for_path(path, environment, now_ms=current_ms)
    if staging is not None:
        return _STAGING_RULE
    orphan = _orphan_root_for_path(path, environment, now_ms=current_ms)
    if orphan is not None:
        return _ORPHAN_RULE
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
        if not _plain_directory_chain(entry, directory):
            continue
        accepted.append((PureWindowsPath(str(entry)), _STAGING_RULE))
    accepted.sort(key=lambda item: str(item[0]).casefold())
    return tuple(accepted)


def claude_plugin_orphan_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
    *,
    now_ms: int | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return expired, unreferenced version dirs with a registered replacement."""

    cache_root = _plugin_cache_root(environment)
    if cache_root is None:
        return ()
    references = _reference_state(environment)
    if not references.valid:
        return ()
    current_ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    directory = Path(str(cache_root))
    accepted: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    try:
        marketplaces = tuple(directory.iterdir())
    except OSError:
        return ()
    for marketplace in marketplaces:
        if not _is_plain_directory(marketplace):
            continue
        try:
            plugins = tuple(marketplace.iterdir())
        except OSError:
            continue
        for plugin in plugins:
            if not _is_plain_directory(plugin):
                continue
            try:
                versions = tuple(plugin.iterdir())
            except OSError:
                continue
            for version in versions:
                candidate = PureWindowsPath(str(version))
                if not _is_expired_unreferenced_orphan(
                    candidate,
                    environment,
                    now_ms=current_ms,
                ):
                    continue
                accepted.append((candidate, _ORPHAN_RULE))
    accepted.sort(key=lambda item: str(item[0]).casefold())
    return tuple(accepted)


def claude_plugin_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    return (
        *claude_plugin_staging_audited_tool_roots(environment),
        *claude_plugin_orphan_audited_tool_roots(environment),
    )


def clear_claude_plugin_process_cache() -> None:
    _reference_state_cached.cache_clear()
    _ORIGINAL_CLEAR_CLAUDE_PROCESS_CACHE()


def install() -> None:
    """Install matcher/root extensions before the application facade snapshots them."""

    if getattr(_claude, "_devclean_plugin_cache_rules", False):
        return
    _claude.match_claude_rule = match_claude_rule
    _claude.clear_claude_process_cache = clear_claude_plugin_process_cache
    vars(_claude)["_devclean_plugin_cache_rules"] = True

    # Import the facade only after Claude callables are patched so its local
    # references use these source-specific wrappers.
    from devclean.core import application_cleanup as application_cleanup

    original_dynamic = application_cleanup.audited_dynamic_tool_roots

    def audited_dynamic_tool_roots(
        environment: Mapping[str, str] | None = None,
    ) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
        return (
            *original_dynamic(environment),
            *claude_plugin_audited_tool_roots(environment),
        )

    application_cleanup.audited_dynamic_tool_roots = audited_dynamic_tool_roots


install()


__all__ = [
    "claude_plugin_audited_tool_roots",
    "claude_plugin_orphan_audited_tool_roots",
    "claude_plugin_staging_audited_tool_roots",
    "clear_claude_plugin_process_cache",
    "install",
    "match_claude_rule",
]
