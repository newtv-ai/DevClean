"""Audited Android Emulator AVD storage semantics for Windows cleanup.

An AVD content directory is user-owned virtual-device state, not a generic cache.
It can contain installed application data, settings, databases, files, SD-card
content, encryption state and snapshots that preserve the entire virtual device.
DevClean therefore protects the content tree by default. The only generic cleanup
candidate is an uncoupled default ``cache.img`` partition: Android documents that
partition as temporary download cache and the emulator recreates it empty.
"""

from __future__ import annotations

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
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    effective_idle_days,
)

_MIB = 1024**2


@dataclass(frozen=True, slots=True)
class AndroidAvdRootSet:
    registry_roots: tuple[PureWindowsPath, ...]
    content_roots: tuple[PureWindowsPath, ...]


def _rule(
    rule_id: str,
    root_key: str,
    relative: str,
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
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="android_avd",
        root_key=root_key,
        relative_pattern=relative,
        match_kind=match_kind,
        owner=owner,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=False,
        user_age_buckets=user_age_buckets,
        label=label,
    )


# The documented default cache partition contains temporary download/cache data
# and is normally deleted on virtual-device power-off. A seven-day idle floor is
# deliberately stricter than the vendor lifecycle so a persisted cache is not
# immediately removed merely because it is large.
_AVD_TEMP_PARTITION_RULE = _rule(
    "android-avd-temporary-partition",
    "ANDROID_AVD_CONTENT",
    "cache.img",
    MatchKind.EXACT,
    DecisionOwner.TOOL,
    RebuildCost.LOW,
    "Android Emulator temporary cache partition",
    idle_days=7,
    min_reclaim_bytes=16 * _MIB,
    requires_process_closed=True,
    size_sensitive_idle=False,
)

# Modern emulator storage may couple cache.img to a qcow2 overlay. DevClean does
# not have a two-file atomic capability, so either member becomes protected when
# that overlay exists. Wipe Data remains the vendor-owned action for the pair.
_AVD_COUPLED_CACHE_RULE = _rule(
    "android-avd-coupled-cache-state",
    "ANDROID_AVD_CONTENT",
    "cache.img",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Android Emulator cache image coupled to a qcow2 overlay",
)
_AVD_CACHE_OVERLAY_RULE = _rule(
    "android-avd-cache-overlay-state",
    "ANDROID_AVD_CONTENT",
    "cache.img.qcow2",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Android Emulator cache qcow2 overlay",
)

_AVD_CONFIG_RULE = _rule(
    "android-avd-config-state",
    "ANDROID_AVD_CONTENT",
    "config.ini",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Android Virtual Device configuration",
)
_AVD_ENCRYPTION_RULE = _rule(
    "android-avd-encryption-state",
    "ANDROID_AVD_CONTENT",
    "encryptionkey.img*",
    MatchKind.GLOB,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Android Virtual Device encryption state coupled to user data",
)
_AVD_USERDATA_RULE = _rule(
    "android-avd-user-data",
    "ANDROID_AVD_CONTENT",
    "userdata*.img*",
    MatchKind.GLOB,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Virtual Device installed apps, settings, databases and files",
    user_age_buckets=(30, 90, 180),
)
_AVD_SDCARD_RULE = _rule(
    "android-avd-sd-card-data",
    "ANDROID_AVD_CONTENT",
    "sdcard*.img*",
    MatchKind.GLOB,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Virtual Device simulated SD-card data",
    user_age_buckets=(30, 90, 180),
)
_AVD_SNAPSHOT_DIR_RULE = _rule(
    "android-avd-snapshot-state",
    "ANDROID_AVD_CONTENT",
    "snapshots",
    MatchKind.PREFIX,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Emulator snapshots preserving complete virtual-device state",
    user_age_buckets=(30, 90, 180),
)
_AVD_SNAPSHOT_FILE_RULE = _rule(
    "android-avd-snapshot-storage",
    "ANDROID_AVD_CONTENT",
    "snapshots*.img*",
    MatchKind.GLOB,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Emulator snapshot storage image",
    user_age_buckets=(30, 90, 180),
)
_AVD_MUTABLE_SYSTEM_RULE = _rule(
    "android-avd-mutable-system-state",
    "ANDROID_AVD_CONTENT",
    "{system,vendor}*.img*",
    MatchKind.GLOB,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Virtual Device writable system or vendor overlay state",
    user_age_buckets=(30, 90, 180),
)
_AVD_DATA_DIR_RULE = _rule(
    "android-avd-data-state",
    "ANDROID_AVD_CONTENT",
    "data",
    MatchKind.PREFIX,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Virtual Device mutable data directory",
    user_age_buckets=(30, 90, 180),
)
_AVD_CONTENT_STATE_RULE = _rule(
    "android-avd-content-state",
    "ANDROID_AVD_CONTENT",
    "",
    MatchKind.PREFIX,
    DecisionOwner.USER,
    RebuildCost.HIGH,
    "Android Virtual Device mutable content and device state",
    user_age_buckets=(30, 90, 180),
)
_AVD_REGISTRY_RULE = _rule(
    "android-avd-registry-state",
    "ANDROID_AVD_REGISTRY",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Android Virtual Device registry and content-location metadata",
)

ANDROID_AVD_RULES: tuple[ApplicationCleanupRule, ...] = (
    _AVD_TEMP_PARTITION_RULE,
    _AVD_COUPLED_CACHE_RULE,
    _AVD_CACHE_OVERLAY_RULE,
    _AVD_CONFIG_RULE,
    _AVD_ENCRYPTION_RULE,
    _AVD_USERDATA_RULE,
    _AVD_SDCARD_RULE,
    _AVD_SNAPSHOT_DIR_RULE,
    _AVD_SNAPSHOT_FILE_RULE,
    _AVD_MUTABLE_SYSTEM_RULE,
    _AVD_DATA_DIR_RULE,
    _AVD_CONTENT_STATE_RULE,
    _AVD_REGISTRY_RULE,
)


def android_avd_roots(
    environment: Mapping[str, str] | None = None,
) -> AndroidAvdRootSet:
    """Discover AVD registry roots and source-backed content directories.

    Current emulator search order includes ``ANDROID_AVD_HOME``, the emulator/user
    home ``avd`` directory, and the default ``%USERPROFILE%\.android\avd``. Older
    tools used ``ANDROID_SDK_HOME`` for the user-specific Android configuration
    root, so it is retained as a compatibility discovery source.
    """

    env = _casefold_env(environment)
    registries: list[PureWindowsPath] = []

    _append_absolute(registries, env.get("android_avd_home"))
    _append_absolute(registries, env.get("devclean_android_avd_home"))

    emulator_home = env.get("android_emulator_home")
    if emulator_home:
        _append_absolute(registries, str(PureWindowsPath(emulator_home) / "avd"))

    android_user_home = env.get("android_user_home")
    if android_user_home:
        _append_absolute(registries, str(PureWindowsPath(android_user_home) / "avd"))

    userprofile = env.get("userprofile")
    if userprofile:
        _append_absolute(
            registries,
            str(PureWindowsPath(userprofile) / ".android" / "avd"),
        )

    legacy_sdk_home = env.get("android_sdk_home")
    if legacy_sdk_home:
        _append_absolute(
            registries,
            str(PureWindowsPath(legacy_sdk_home) / ".android" / "avd"),
        )

    registry_roots = _unique_paths(registries)
    contents: list[PureWindowsPath] = []
    for registry in registry_roots:
        _append_registry_content_roots(registry, contents)

    explicit_content = env.get("devclean_android_avd_content_dir")
    _append_absolute(contents, explicit_content)

    return AndroidAvdRootSet(
        registry_roots=registry_roots,
        content_roots=_unique_paths(contents),
    )


def android_avd_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    roots = android_avd_roots(environment)
    return _minimal_roots((*roots.registry_roots, *roots.content_roots))


def match_android_avd_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    roots = android_avd_roots(environment)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for content in roots.content_roots:
        content_norm = _impl._normalize(content)
        if not _impl._matches(normalized, content_norm, MatchKind.PREFIX):
            continue

        cache = content / "cache.img"
        overlay = content / "cache.img.qcow2"
        if normalized == _impl._normalize(cache):
            rule = (
                _AVD_COUPLED_CACHE_RULE
                if _path_exists(overlay)
                else _AVD_TEMP_PARTITION_RULE
            )
            _append_match(matches, normalized, content, rule, 0)
        elif normalized == _impl._normalize(overlay):
            _append_match(matches, normalized, content, _AVD_CACHE_OVERLAY_RULE, 0)

        for index, rule in enumerate(
            (
                _AVD_CONFIG_RULE,
                _AVD_ENCRYPTION_RULE,
                _AVD_USERDATA_RULE,
                _AVD_SDCARD_RULE,
                _AVD_SNAPSHOT_DIR_RULE,
                _AVD_SNAPSHOT_FILE_RULE,
                _AVD_MUTABLE_SYSTEM_RULE,
                _AVD_DATA_DIR_RULE,
                _AVD_CONTENT_STATE_RULE,
            )
        ):
            _append_match(matches, normalized, content, rule, index + 100)

    # Registry metadata is protected, but content directories nested below the
    # registry have already contributed longer/more-specific matches above.
    for registry in roots.registry_roots:
        _append_match(matches, normalized, registry, _AVD_REGISTRY_RULE, 10_000)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def android_avd_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """AVD cleanup is file-scoped; no AVD directory has whole-tree authority."""

    del environment
    return ()


def whole_tree_android_avd_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    del path, environment
    return None


def evaluate_android_avd_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_android_avd_rule(path, environment)
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
            rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0
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
        running = android_avd_process_running()
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
def android_avd_process_running() -> bool:
    """Return whether any Android Emulator/QEMU virtual device is active."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(?i:emulator(?:64)?(?:-[^.]*)?\\.exe|qemu-system-[^.]+'"
        "+'\\.exe)$' }; if ($p) { 'RUNNING' }"
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


def clear_android_avd_process_cache() -> None:
    android_avd_process_running.cache_clear()


def _append_registry_content_roots(
    registry: PureWindowsPath,
    contents: list[PureWindowsPath],
) -> None:
    registry_path = Path(str(registry))
    try:
        children = tuple(registry_path.iterdir())
    except OSError:
        return

    for child in children:
        try:
            if child.is_dir() and child.name.casefold().endswith(".avd"):
                contents.append(PureWindowsPath(str(child)))
                continue
        except OSError:
            continue
        if not child.is_file() or child.suffix.casefold() != ".ini":
            continue
        configured = _content_path_from_root_ini(child, registry)
        if configured is not None:
            contents.append(configured)


def _content_path_from_root_ini(
    path: Path,
    registry: PureWindowsPath,
) -> PureWindowsPath | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().casefold()] = value.strip().strip('"').strip("'")

    absolute = values.get("path")
    if absolute:
        candidate = PureWindowsPath(absolute)
        if candidate.is_absolute():
            return candidate

    relative = values.get("path.rel")
    if not relative:
        return None
    candidate = PureWindowsPath(relative)
    if candidate.is_absolute():
        return candidate
    # AOSP defines path.rel relative to the user Android configuration path;
    # the AVD registry is that path's ``avd`` child.
    return PureWindowsPath(registry).parent / candidate


def _append_match(
    matches: list[tuple[int, int, ApplicationCleanupRule]],
    normalized_path: str,
    root: PureWindowsPath,
    rule: ApplicationCleanupRule,
    index: int,
) -> None:
    normalized_root = _impl._normalize(root)
    relative = rule.relative_pattern
    candidate = normalized_root + ("\\" + relative if relative else "")
    if not _impl._matches(normalized_path, candidate, rule.match_kind):
        return
    if rule.owner is DecisionOwner.KEEP:
        owner_weight = 3
    elif rule.owner is DecisionOwner.USER:
        owner_weight = 2
    else:
        owner_weight = 1
    matches.append((len(candidate), owner_weight * 1000 - index, rule))


def _path_exists(path: PureWindowsPath) -> bool:
    try:
        return Path(str(path)).exists()
    except OSError:
        # Uncertain coupling must fail closed.
        return True


def _append_absolute(found: list[PureWindowsPath], value: str | None) -> None:
    if not value:
        return
    candidate = PureWindowsPath(value.strip().strip('"').strip("'"))
    if candidate.is_absolute():
        found.append(candidate)


def _unique_paths(paths: list[PureWindowsPath]) -> tuple[PureWindowsPath, ...]:
    found: list[PureWindowsPath] = []
    seen: set[str] = set()
    for path in paths:
        key = _impl._normalize(path)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(path)
    return tuple(found)


def _minimal_roots(paths: tuple[PureWindowsPath, ...]) -> tuple[PureWindowsPath, ...]:
    ordered = sorted(_unique_paths(list(paths)), key=lambda item: len(item.parts))
    found: list[PureWindowsPath] = []
    for path in ordered:
        normalized = _impl._normalize(path)
        if any(
            normalized == _impl._normalize(parent)
            or normalized.startswith(_impl._normalize(parent) + "\\")
            for parent in found
        ):
            continue
        found.append(path)
    return tuple(found)


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "ANDROID_AVD_RULES",
    "AndroidAvdRootSet",
    "android_avd_audited_tool_roots",
    "android_avd_process_running",
    "android_avd_roots",
    "android_avd_scan_roots",
    "clear_android_avd_process_cache",
    "evaluate_android_avd_path",
    "match_android_avd_rule",
    "whole_tree_android_avd_rule",
]
