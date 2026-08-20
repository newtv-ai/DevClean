"""Audited Gradle User Home storage semantics for Windows cleanup.

Gradle User Home is mixed, executable-policy-sensitive state. It contains user
configuration and init scripts, downloaded dependencies/toolchains/wrapper
distributions, daemon coordination state, and caches whose retention is owned by
Gradle's configurable garbage collector. DevClean inventories these locations but
does not derive raw deletion authority from directory names, age, size, or a
partial static scan of init scripts.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
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
)

# These names are useful for inventory/explanation only. Gradle's own cleanup
# decisions use effective CacheConfigurations plus usage markers and version
# relationships, so matching one of these names never grants mutation authority.
_VERSION_DIR_RE = re.compile(
    r"^\d+(?:\.\d+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.+-]*)?$",
    re.IGNORECASE,
)
_SNAPSHOT_VERSION_RE = re.compile(
    r"-\d{8,}(?:\+\d{4})?(?:[-+].*)?$",
    re.IGNORECASE,
)
_BUILD_CACHE_RE = re.compile(r"^build-cache-\d+$", re.IGNORECASE)

# Gradle is normally a java/javaw process. Restrict the command-line match to
# Gradle launch/daemon/wrapper entry points so unrelated Java applications do not
# affect process-state reporting or redirected-user-home discovery.
_GRADLE_COMMAND_RE = (
    r"(?i)(?:org\.gradle\.launcher\.daemon\.bootstrap\.GradleDaemon|"
    r"org\.gradle\.launcher\.GradleMain|org\.gradle\.wrapper\.GradleWrapperMain)"
)


def _rule(
    rule_id: str,
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
    allow_whole_tree: bool = False,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="gradle",
        root_key="GRADLE_USER_HOME",
        relative_pattern=relative,
        match_kind=match_kind,
        owner=owner,
        last_use=(
            LastUseStrategy.FILE_MTIME
            if match_kind is MatchKind.GLOB
            else LastUseStrategy.DIRECTORY_MTIME
        ),
        rebuild_cost=rebuild_cost,
        idle_days=idle_days,
        min_reclaim_bytes=min_reclaim_bytes,
        requires_process_closed=requires_process_closed,
        size_sensitive_idle=size_sensitive_idle,
        allow_whole_tree=allow_whole_tree,
        label=label,
    )


# Gradle User Home is reportable mixed state. Dynamic version/build-cache rules
# below only make inventory labels more precise; every rule remains KEEP-owned.
_GRADLE_PROPERTIES_RULE = _rule(
    "gradle-user-properties",
    "gradle.properties",
    MatchKind.EXACT,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle user configuration and possible credentials",
)
_GRADLE_INIT_RULE = _rule(
    "gradle-init-scripts",
    "init.d",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle user initialization scripts and executable cache policy",
)
_GRADLE_WRAPPER_RULE = _rule(
    "gradle-wrapper-distributions",
    "wrapper",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle Wrapper distributions retained for project and offline reuse",
)
_GRADLE_TOOLCHAINS_RULE = _rule(
    "gradle-downloaded-jdks",
    "jdks",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle provisioned Java toolchains",
)
_GRADLE_SHARED_CACHES_RULE = _rule(
    "gradle-shared-cache-state",
    "caches",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle shared dependency, transform, metadata and cache state",
)
_GRADLE_DAEMON_STATE_RULE = _rule(
    "gradle-daemon-state",
    "daemon",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.MEDIUM,
    "Gradle daemon registry and coordination state",
)
_GRADLE_DAEMON_LOG_RULE = _rule(
    "gradle-daemon-log",
    r"daemon\*\daemon-*.out.log",
    MatchKind.GLOB,
    DecisionOwner.KEEP,
    RebuildCost.NONE,
    "Gradle daemon diagnostic log managed by configurable vendor retention",
)
_GRADLE_ROOT_STATE_RULE = _rule(
    "gradle-user-home-state",
    "",
    MatchKind.PREFIX,
    DecisionOwner.KEEP,
    RebuildCost.HIGH,
    "Gradle User Home mixed configuration, downloads and vendor-managed cache state",
)

GRADLE_RULES: tuple[ApplicationCleanupRule, ...] = (
    _GRADLE_PROPERTIES_RULE,
    _GRADLE_INIT_RULE,
    _GRADLE_WRAPPER_RULE,
    _GRADLE_TOOLCHAINS_RULE,
    _GRADLE_SHARED_CACHES_RULE,
    _GRADLE_DAEMON_STATE_RULE,
    _GRADLE_DAEMON_LOG_RULE,
    _GRADLE_ROOT_STATE_RULE,
)


def gradle_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return documented and source-backed Gradle User Home locations.

    The documented default is retained even when an override is active so stale
    storage from a former setup remains discoverable. Explicit environment/system
    property and running command-line overrides are additional audited roots.
    Discovery never executes Gradle, project code, settings, or init scripts.
    """

    env = _casefold_env(environment)
    found: list[PureWindowsPath] = []

    userprofile = env.get("userprofile")
    if userprofile:
        _append_absolute(found, str(PureWindowsPath(userprofile) / ".gradle"))

    _append_absolute(found, env.get("gradle_user_home"))
    _append_absolute(found, env.get("devclean_gradle_user_home"))

    gradle_opts = env.get("gradle_opts")
    if gradle_opts:
        _append_absolute(found, _system_property_value(gradle_opts, "gradle.user.home"))

    if environment is None:
        for path in _running_override_paths():
            _append_absolute(found, path)

    return _unique_paths(found)


def gradle_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    return gradle_roots(environment)


def match_gradle_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    normalized = _impl._normalize(path)
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []

    for root in gradle_roots(environment):
        root_norm = _impl._normalize(root)
        if not _impl._matches(normalized, root_norm, MatchKind.PREFIX):
            continue

        # Dynamic cache-directory recognition is classification only. It does not
        # inspect or execute init scripts and cannot create whole-tree authority.
        for dynamic_root, rule in _dynamic_report_roots_for(root):
            _append_match(matches, normalized, dynamic_root.parent.parent, rule, 0)

        for index, rule in enumerate(GRADLE_RULES):
            _append_match(matches, normalized, root, rule, index + 100)

    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def gradle_audited_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """No generic Gradle User Home delete root is currently source-authorized."""

    del environment
    return ()


def whole_tree_gradle_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Raw whole-tree Gradle User Home cache deletion is not authorized."""

    del path, environment
    return None


def evaluate_gradle_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    del logical_size, process_running
    rule = match_gradle_rule(path, environment)
    if rule is None:
        return None

    current = _impl._as_utc(now or datetime.now(UTC))
    assert current is not None
    observed = _impl._as_utc(last_used)
    idle = None if observed is None else max(0.0, (current - observed).total_seconds() / 86_400)
    return ApplicationPolicyDecision(rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0)


@lru_cache(maxsize=1)
def gradle_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(?i:javaw?\\.exe)$' -and "
        f"$_.CommandLine -match '{_GRADLE_COMMAND_RE}' }}; "
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
def _running_override_paths() -> tuple[str, ...]:
    if os.name != "nt":
        return ()
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match '^(?i:javaw?\\.exe)$' -and "
        f"$_.CommandLine -match '{_GRADLE_COMMAND_RE}' }}; "
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
        value = _gradle_user_home_from_command_line(line)
        if value:
            found.append(value)
    return tuple(dict.fromkeys(found))


def clear_gradle_process_cache() -> None:
    gradle_process_running.cache_clear()
    _running_override_paths.cache_clear()


def _dynamic_report_roots_for(
    root: PureWindowsPath,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    cache_parent = root / "caches"
    try:
        children = tuple(Path(str(cache_parent)).iterdir())
    except OSError:
        return ()

    found: list[tuple[PureWindowsPath, ApplicationCleanupRule]] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        name = child.name
        if _BUILD_CACHE_RE.fullmatch(name):
            rule = _dynamic_cache_rule(
                "gradle-local-build-cache",
                name,
                rebuild_cost=RebuildCost.MEDIUM,
                label="Gradle local build cache managed by effective vendor policy",
            )
        elif _VERSION_DIR_RE.fullmatch(name):
            snapshot = _SNAPSHOT_VERSION_RE.search(name) is not None
            rule = _dynamic_cache_rule(
                ("gradle-snapshot-version-cache" if snapshot else "gradle-release-version-cache"),
                name,
                rebuild_cost=RebuildCost.HIGH,
                label=(
                    f"Gradle snapshot {name} version-specific vendor-managed cache"
                    if snapshot
                    else f"Gradle {name} version-specific vendor-managed cache"
                ),
            )
        else:
            continue
        found.append((cache_parent / name, rule))
    return tuple(found)


def _dynamic_cache_rule(
    rule_id: str,
    cache_name: str,
    *,
    rebuild_cost: RebuildCost,
    label: str,
) -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id=rule_id,
        app_id="gradle",
        root_key="GRADLE_USER_HOME",
        relative_pattern=str(PureWindowsPath("caches") / cache_name),
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.KEEP,
        last_use=LastUseStrategy.DIRECTORY_MTIME,
        rebuild_cost=rebuild_cost,
        idle_days=None,
        min_reclaim_bytes=0,
        requires_process_closed=False,
        size_sensitive_idle=False,
        allow_whole_tree=False,
        label=label,
    )


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


def _gradle_user_home_from_command_line(command_line: str) -> str | None:
    system = _system_property_value(command_line, "gradle.user.home")
    if system:
        return system
    pattern = re.compile(
        r"""(?:^|\s)(?:--gradle-user-home|-g)(?:=|\s+)(?:"([^"]+)"|'([^']+)'|([^\s]+))""",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


def _system_property_value(command_line: str, key: str) -> str | None:
    pattern = re.compile(
        rf"""(?:^|\s)-D{re.escape(key)}=(?:"([^"]+)"|'([^']+)'|([^\s]+))""",
        re.IGNORECASE,
    )
    match = pattern.search(command_line)
    if not match:
        return None
    return next((group for group in match.groups() if group), None)


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
    "GRADLE_RULES",
    "clear_gradle_process_cache",
    "evaluate_gradle_path",
    "gradle_audited_tool_roots",
    "gradle_process_running",
    "gradle_roots",
    "gradle_scan_roots",
    "match_gradle_rule",
    "whole_tree_gradle_rule",
]
