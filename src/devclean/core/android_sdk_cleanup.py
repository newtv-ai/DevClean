"""Audited Android SDK installation storage semantics for Windows cleanup.

An Android SDK root is primarily installed developer-tool payload, not a cache.
Platforms, build tools, emulator/system images, NDKs, licenses, command-line tools
and package metadata can all be required by existing projects. DevClean therefore
protects the SDK root by default and grants generic whole-tree cleanup authority
only to the exact installer ``temp`` directory that AOSP defines for downloads
and archive extraction during SDK package installation.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
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

# SDK package installation can be driven by Android Studio, sdkmanager, or the
# Android Gradle Plugin's automatic SDK download path. Only those Java/Studio
# processes block the temp-root cleanup; unrelated Java programs do not.
_SDK_WRITER_COMMAND_RE = (
    r"(?i)(?:com\.android\.sdklib\.tool\.sdkmanager\.SdkManagerCli|"
    r"com\.android\.sdkmanager\.Main|"
    r"org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon|"
    r"org\.gradle\.launcher\.GradleMain|"
    r"org\.gradle\.wrapper\.GradleWrapperMain)"
)


def _rule(
    rule_id: str,
    relative: str,
    owner: DecisionOwner,
    rebuild_cost: RebuildCost,
    label: str,
    *,
    idle_days: float | None = None,
    min_reclaim_bytes: int = 0,
    requires_process_closed: bool = False,
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="android_sdk",
        root_key="ANDROID_SDK_ROOT",
        relative_pattern=relative,
        match_kind=MatchKind.PREFIX,
        owner=owner,
        last_use=LastUseStrategy.DIRECTORY_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=False,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


# AOSP RepoConstants defines SDK_ROOT/temp as the temporary folder used to hold
# downloads and extracted archives during package installation. No vendor TTL is
# published, so DevClean adds a conservative seven-day idle floor and never
# touches it while Studio/sdkmanager/Gradle may still be writing the SDK.
_ANDROID_SDK_TEMP_RULE = _rule(
    "android-sdk-install-temp",
    "temp",
    DecisionOwner.TOOL,
    RebuildCost.LOW,
    "Android SDK package installation temporary downloads and extraction",
    idle_days=7,
    min_reclaim_bytes=32 * _MIB,
    requires_process_closed=True,
    allow_whole_tree=True,
)
_ANDROID_SDK_LICENSES_RULE = _rule(
    "android-sdk-licenses",
    "licenses",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Android SDK accepted package licenses required for automated installs",
)
_ANDROID_SDK_SYSTEM_IMAGES_RULE = _rule(
    "android-sdk-system-images",
    "system-images",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Installed Android Emulator system images",
)
_ANDROID_SDK_EMULATOR_RULE = _rule(
    "android-sdk-emulator",
    "emulator",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Installed Android Emulator package",
)
_ANDROID_SDK_NDK_RULE = _rule(
    "android-sdk-ndk",
    "ndk",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Installed Android NDK packages",
)
_ANDROID_SDK_ROOT_RULE = _rule(
    "android-sdk-installed-payload",
    "",
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Android SDK installed packages, tools and package metadata",
)

ANDROID_SDK_RULES: tuple[ApplicationCleanupRule, ...] = (
    _ANDROID_SDK_TEMP_RULE,
    _ANDROID_SDK_LICENSES_RULE,
    _ANDROID_SDK_SYSTEM_IMAGES_RULE,
    _ANDROID_SDK_EMULATOR_RULE,
    _ANDROID_SDK_NDK_RULE,
    _ANDROID_SDK_ROOT_RULE,
)


def android_sdk_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return source-backed Android SDK installation roots for Windows.

    The Android Studio default is retained even when an environment override is
    present, so an old/default SDK remains protected and its stale installer temp
    can still be inventoried. ``ANDROID_SDK_ROOT`` is deprecated by Google but is
    retained as a compatibility discovery source.
    """

    env = _casefold_env(environment)
    found: list[PureWindowsPath] = []

    localappdata = env.get("localappdata")
    if localappdata:
        _append_absolute(found, str(PureWindowsPath(localappdata) / "Android" / "Sdk"))

    _append_absolute(found, env.get("android_home"))
    _append_absolute(found, env.get("android_sdk_root"))
    _append_absolute(found, env.get("devclean_android_sdk_root"))

    if environment is None:
        for path in _running_sdk_roots():
            _append_absolute(found, path)

    return _unique_paths(found)


def android_sdk_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return android_sdk_roots(environment)


def match_android_sdk_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for root in android_sdk_roots(environment):
        for index, rule in enumerate(ANDROID_SDK_RULES):
            _append_match(matches, normalized, root, rule, index)
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def android_sdk_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    seen: set[str] = set()
    for root in android_sdk_roots(environment):
        path = root / _ANDROID_SDK_TEMP_RULE.relative_pattern
        key = _impl._normalize(path)
        if key in seen:
            continue
        seen.add(key)
        found.append((path, _ANDROID_SDK_TEMP_RULE))
    return tuple(found)


def whole_tree_android_sdk_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    target = _impl._normalize(path)
    for root, rule in android_sdk_audited_tool_roots(environment):
        if target == _impl._normalize(root):
            return rule
    return None


def evaluate_android_sdk_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    rule = match_android_sdk_rule(path, environment)
    if rule is None:
        return None

    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0)

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = android_sdk_process_running()
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
def android_sdk_process_running() -> bool:
    """Return whether a known SDK-writing process is active; fail closed."""

    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(?i:studio64?\\.exe)$' -or "
        "($_.Name -match '^(?i:javaw?\\.exe)$' -and "
        f"$_.CommandLine -match '{_SDK_WRITER_COMMAND_RE}') }}; "
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


@lru_cache(maxsize=1)
def _running_sdk_roots() -> tuple[str, ...]:
    """Collect explicit ``sdkmanager --sdk_root`` roots from running Java CLIs."""

    if os.name != "nt":
        return ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(?i:javaw?\\.exe)$' -and "
        "$_.CommandLine -match '(?i)(?:SdkManagerCli|com\\.android\\.sdkmanager\\.Main)' }; "
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
        return ()
    if result.returncode != 0:
        return ()

    found: list[str] = []
    for line in result.stdout.splitlines():
        value = _sdk_root_from_command_line(line)
        if value:
            found.append(value)
    return tuple(dict.fromkeys(found))


def clear_android_sdk_process_cache() -> None:
    android_sdk_process_running.cache_clear()
    _running_sdk_roots.cache_clear()


def _sdk_root_from_command_line(command_line: str) -> str | None:
    pattern = re.compile(
        r"""(?:^|\s)--sdk_root(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s]+))""",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


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
    owner_weight = 3 if rule.owner is DecisionOwner.KEEP else 1
    matches.append((len(candidate), owner_weight * 1000 - index, rule))


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


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "ANDROID_SDK_RULES",
    "android_sdk_audited_tool_roots",
    "android_sdk_process_running",
    "android_sdk_roots",
    "android_sdk_scan_roots",
    "clear_android_sdk_process_cache",
    "evaluate_android_sdk_path",
    "match_android_sdk_rule",
    "whole_tree_android_sdk_rule",
]
