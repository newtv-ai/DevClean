"""Application-aware cleanup policy with usage and reclaim-benefit semantics.

Generic filename heuristics are intentionally weaker than this catalog.  An
application profile can say that data is regenerable, user-owned history, or
persistent state, and can attach an idle threshold, rebuild cost, process guard,
and minimum reclaim value.  The scanner/classifier may then make a useful
cleanup recommendation without pretending that file age itself determines
safety.

Codex is the first fully-audited profile.  Future application profiles (Claude
Code, Cursor, Trae, and others) should use the same types and evaluator rather
than adding one-off path heuristics.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import PureWindowsPath
from typing import Mapping

_MIB = 1024**2
_GIB = 1024**3


class DecisionOwner(StrEnum):
    """Who is allowed to decide that an application item should be removed."""

    TOOL = "TOOL"
    USER = "USER"
    KEEP = "KEEP"


class LastUseStrategy(StrEnum):
    """Best available source for an item's last meaningful use time."""

    FILE_MTIME = "FILE_MTIME"
    DIRECTORY_MTIME = "DIRECTORY_MTIME"
    APP_ACTIVITY = "APP_ACTIVITY"
    SESSION_LAST_EVENT = "SESSION_LAST_EVENT"
    JSONL_RECORD_TS = "JSONL_RECORD_TS"


class RebuildCost(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PolicyAction(StrEnum):
    """Recommendation emitted by the policy engine; never a mutation command."""

    TOOL_DELETE = "TOOL_DELETE"
    TOOL_KEEP_RECENT = "TOOL_KEEP_RECENT"
    TOOL_KEEP_LOW_BENEFIT = "TOOL_KEEP_LOW_BENEFIT"
    TOOL_KEEP_IN_USE = "TOOL_KEEP_IN_USE"
    TOOL_KEEP_UNKNOWN_USAGE = "TOOL_KEEP_UNKNOWN_USAGE"
    USER_DECISION = "USER_DECISION"
    KEEP_PROTECTED = "KEEP_PROTECTED"


class MatchKind(StrEnum):
    EXACT = "EXACT"
    PREFIX = "PREFIX"
    GLOB = "GLOB"


@dataclass(frozen=True, slots=True)
class ApplicationCleanupRule:
    rule_id: str
    app_id: str
    root_key: str
    relative_pattern: str
    match_kind: MatchKind
    owner: DecisionOwner
    last_use: LastUseStrategy
    rebuild_cost: RebuildCost
    idle_days: float | None = None
    min_reclaim_bytes: int = 0
    requires_process_closed: bool = False
    size_sensitive_idle: bool = True
    user_age_buckets: tuple[int, ...] = ()
    allow_whole_tree: bool = False
    label: str = ""


@dataclass(frozen=True, slots=True)
class ApplicationPolicyDecision:
    rule: ApplicationCleanupRule
    action: PolicyAction
    last_used: datetime | None
    idle_days: float | None
    effective_idle_days: float | None
    benefit_score: int
    age_bucket: str | None = None

    @property
    def requires_process_closed(self) -> bool:
        return self.rule.requires_process_closed


@dataclass(frozen=True, slots=True)
class ApplicationRoot:
    key: str
    path: PureWindowsPath


# These are deliberately semantic rules rather than broad suffix rules.  A
# directory named "cache" is not automatically disposable: Codex's
# ``plugins/cache`` is the active installed-plugin store and is therefore kept.
CODEX_RULES: tuple[ApplicationCleanupRule, ...] = (
    # Small remote catalogs: safe to regenerate, but usually too small to be
    # worth deleting.  The 1 MiB floor prevents churn for a few KB of savings.
    ApplicationCleanupRule(
        "codex-model-catalog",
        "codex",
        "CODEX_HOME",
        "models_cache.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        idle_days=7,
        min_reclaim_bytes=_MIB,
        label="Codex model catalog cache",
    ),
    ApplicationCleanupRule(
        "codex-cloud-config-cache",
        "codex",
        "CODEX_HOME",
        "cloud-config-bundle-cache.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        idle_days=7,
        min_reclaim_bytes=_MIB,
        label="Codex cloud configuration cache",
    ),
    ApplicationCleanupRule(
        "codex-version-cache",
        "codex",
        "CODEX_HOME",
        "version.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        idle_days=7,
        min_reclaim_bytes=_MIB,
        label="Codex update-check cache",
    ),
    # Downloaded curated-plugin marketplace snapshot.  It is fully regenerable
    # but re-downloads cost time/network, so keep it while Codex is active.
    ApplicationCleanupRule(
        "codex-plugin-marketplace-snapshot",
        "codex",
        "CODEX_HOME",
        r".tmp\plugins",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.MEDIUM,
        idle_days=30,
        min_reclaim_bytes=16 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
        label="Codex curated plugin marketplace snapshot",
    ),
    ApplicationCleanupRule(
        "codex-plugin-sync-marker",
        "codex",
        "CODEX_HOME",
        r".tmp\plugins.sha",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Codex plugin sync marker",
    ),
    ApplicationCleanupRule(
        "codex-plugin-sync-lock",
        "codex",
        "CODEX_HOME",
        r".tmp\plugins.sync.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Codex plugin synchronization lock",
    ),
    ApplicationCleanupRule(
        "codex-rollout-maintenance-lock",
        "codex",
        "CODEX_HOME",
        r".tmp\rollout-maintenance.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Codex rollout maintenance lock",
    ),
    ApplicationCleanupRule(
        "codex-rollout-compression-lock",
        "codex",
        "CODEX_HOME",
        r".tmp\rollout-compression.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=0.25,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Codex rollout compression lock",
    ),
    ApplicationCleanupRule(
        "codex-orphan-rollout-temp",
        "codex",
        "CODEX_HOME",
        r".tmp\*.tmp",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
        label="Codex orphan rollout temporary file",
    ),
    # logs_2.sqlite is diagnostic tracing state.  Deleting the DB group while
    # Codex is closed loses only diagnostics; WAL/SHM must not be touched while
    # the writer is active.
    ApplicationCleanupRule(
        "codex-log-db",
        "codex",
        "CODEX_HOME",
        "logs_2.sqlite*",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=10,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        label="Codex tracing log database",
    ),
    # Canonical user history.  Age is only for grouping; deletion is always the
    # user's choice.  Session content provides a better timestamp than NTFS
    # metadata when a higher layer is able to parse it.
    ApplicationCleanupRule(
        "codex-active-sessions",
        "codex",
        "CODEX_HOME",
        "sessions",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.SESSION_LAST_EVENT,
        RebuildCost.HIGH,
        user_age_buckets=(30, 90, 180),
        label="Codex conversation history",
    ),
    ApplicationCleanupRule(
        "codex-archived-sessions",
        "codex",
        "CODEX_HOME",
        "archived_sessions",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.SESSION_LAST_EVENT,
        RebuildCost.HIGH,
        user_age_buckets=(30, 90, 180),
        label="Codex archived conversation history",
    ),
    ApplicationCleanupRule(
        "codex-input-history",
        "codex",
        "CODEX_HOME",
        "history.jsonl",
        MatchKind.EXACT,
        DecisionOwner.USER,
        LastUseStrategy.JSONL_RECORD_TS,
        RebuildCost.HIGH,
        user_age_buckets=(30, 90, 180),
        label="Codex input history",
    ),
    # Persistent or authoritative state.  The generic classifier must never let
    # suffixes such as .sqlite or a parent named cache override these meanings.
    ApplicationCleanupRule(
        "codex-installed-plugin-store",
        "codex",
        "CODEX_HOME",
        r"plugins\cache",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex installed plugin store",
    ),
    ApplicationCleanupRule(
        "codex-plugin-data",
        "codex",
        "CODEX_HOME",
        r"plugins\data",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex persistent plugin data",
    ),
    ApplicationCleanupRule(
        "codex-state-databases",
        "codex",
        "CODEX_HOME",
        "{state_5.sqlite*,goals_1.sqlite*,memories_1.sqlite*,queue_1.sqlite*,thread_history_1.sqlite*}",
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex persistent state databases",
    ),
    ApplicationCleanupRule(
        "codex-session-index",
        "codex",
        "CODEX_HOME",
        "session_index.jsonl",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex session index",
    ),
    ApplicationCleanupRule(
        "codex-auth",
        "codex",
        "CODEX_HOME",
        "auth.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex authentication state",
    ),
    ApplicationCleanupRule(
        "codex-config",
        "codex",
        "CODEX_HOME",
        "config.toml",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex configuration",
    ),
    # Windows desktop Chromium/Electron-derived data.  Target only known cache
    # subtrees; the profile itself, Preferences, Local State, cookies and local
    # storage are not cache-cleaning targets.
    ApplicationCleanupRule(
        "codex-desktop-crashpad-reports",
        "codex",
        "CODEX_DESKTOP",
        r"Crashpad\{reports,pending}",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        label="Codex desktop crash reports",
    ),
    ApplicationCleanupRule(
        "codex-desktop-browser-cache",
        "codex",
        "CODEX_DESKTOP",
        r"{Cache,Code Cache,GPUCache,GrShaderCache,ShaderCache,DawnCache,DawnWebGPUCache,GraphiteDawnCache,component_crx_cache,extensions_crx_cache}",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        idle_days=30,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
        label="Codex desktop regenerable browser cache",
    ),
    ApplicationCleanupRule(
        "codex-desktop-default-browser-cache",
        "codex",
        "CODEX_DESKTOP",
        r"Default\{Cache,Code Cache,GPUCache,GrShaderCache,ShaderCache,DawnCache,DawnWebGPUCache,GraphiteDawnCache}",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        idle_days=30,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
        label="Codex desktop profile cache",
    ),
    ApplicationCleanupRule(
        "codex-desktop-profile-state",
        "codex",
        "CODEX_DESKTOP",
        r"{Local State,Default\Preferences,Default\Cookies*,Default\Login Data*,Default\Local Storage,Default\Session Storage,Default\IndexedDB}",
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        label="Codex desktop browser profile state",
    ),
)


def application_roots(environment: Mapping[str, str] | None = None) -> tuple[ApplicationRoot, ...]:
    env = _casefold_env(environment)
    roots: list[ApplicationRoot] = []
    codex_home = env.get("codex_home")
    if codex_home:
        roots.append(ApplicationRoot("CODEX_HOME", PureWindowsPath(codex_home)))
    else:
        profile = env.get("userprofile")
        if profile:
            roots.append(ApplicationRoot("CODEX_HOME", PureWindowsPath(profile) / ".codex"))
    local = env.get("localappdata")
    if local:
        roots.append(
            ApplicationRoot(
                "CODEX_DESKTOP",
                PureWindowsPath(local)
                / "Packages"
                / "OpenAI.Codex_2p2nqsd0c76g0"
                / "LocalCache"
                / "Roaming"
                / "Codex"
                / "web"
                / "Codex",
            )
        )
    return tuple(roots)


def match_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return the most-specific audited application rule for *path*."""

    normalized = _normalize(path)
    roots = {root.key: _normalize(root.path) for root in application_roots(environment)}
    matches: list[tuple[int, int, ApplicationCleanupRule]] = []
    for index, rule in enumerate(CODEX_RULES):
        root = roots.get(rule.root_key)
        if root is None:
            continue
        for expanded in _expand_braces(rule.relative_pattern):
            candidate = root + ("\\" + expanded if expanded else "")
            if _matches(normalized, candidate, rule.match_kind):
                # Longest pattern wins; KEEP wins ties so an exact persistent
                # state rule dominates a broader cache subtree.
                owner_weight = 2 if rule.owner is DecisionOwner.KEEP else 1
                matches.append((len(candidate), owner_weight * 1000 - index, rule))
    if not matches:
        return None
    return max(matches, key=lambda item: (item[0], item[1]))[2]


def evaluate_application_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Classify one path using semantic safety plus cleanup benefit.

    ``last_used`` should be the best timestamp available to the caller.  File
    mtime is an acceptable fallback for caches.  Session/history-specific
    parsers can later pass their embedded event timestamps without changing the
    policy model.
    """

    rule = match_application_rule(path, environment)
    if rule is None:
        return None
    current = now or datetime.now(UTC)
    observed = _as_utc(last_used)
    idle = None
    if observed is not None:
        idle = max(0.0, (current - observed).total_seconds() / 86400)

    if rule.owner is DecisionOwner.KEEP:
        return ApplicationPolicyDecision(rule, PolicyAction.KEEP_PROTECTED, observed, idle, None, 0)

    if rule.owner is DecisionOwner.USER:
        return ApplicationPolicyDecision(
            rule,
            PolicyAction.USER_DECISION,
            observed,
            idle,
            None,
            _benefit_score(logical_size, idle, None, rule.rebuild_cost),
            _age_bucket(idle, rule.user_age_buckets),
        )

    threshold = effective_idle_days(rule, logical_size)
    running = process_running
    if running is None and rule.requires_process_closed:
        running = application_process_running(rule.app_id)
    score = _benefit_score(logical_size, idle, threshold, rule.rebuild_cost)
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


def effective_idle_days(rule: ApplicationCleanupRule, logical_size: int) -> float | None:
    """Shorten an idle threshold only when the reclaim value is materially large."""

    base = rule.idle_days
    if base is None or not rule.size_sensitive_idle:
        return base
    if logical_size >= 20 * _GIB:
        return min(base, 7)
    if logical_size >= 5 * _GIB:
        return min(base, 14)
    if logical_size >= _GIB:
        return min(base, 21)
    return base


@lru_cache(maxsize=8)
def application_process_running(app_id: str) -> bool:
    """Best-effort process guard used during one scan/session.

    The mutation layer should call ``clear_process_cache`` and re-check before
    deleting a path whose rule requires the application to be closed.
    """

    if app_id != "codex" or os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # Unknown is treated as running: a failed process check must not widen
        # what the tool is willing to delete.
        return True
    output = result.stdout.casefold()
    return '"codex.exe"' in output or '"chatgpt.exe"' in output


def clear_process_cache() -> None:
    application_process_running.cache_clear()


def process_guard_allows(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Re-check whether an application-guarded target may be mutated now."""

    rule = match_application_rule(path, environment)
    if rule is None or not rule.requires_process_closed:
        return True
    clear_process_cache()
    return not application_process_running(rule.app_id)


def _benefit_score(
    logical_size: int,
    idle_days: float | None,
    threshold: float | None,
    rebuild_cost: RebuildCost,
) -> int:
    if logical_size < _MIB:
        size_score = 0
    elif logical_size < 100 * _MIB:
        size_score = 20
    elif logical_size < _GIB:
        size_score = 40
    elif logical_size < 5 * _GIB:
        size_score = 60
    elif logical_size < 20 * _GIB:
        size_score = 80
    else:
        size_score = 100
    if idle_days is None:
        idle_score = 0
    elif threshold in (None, 0):
        idle_score = 100
    else:
        idle_score = min(100, round(100 * idle_days / threshold))
    penalty = {
        RebuildCost.NONE: 0,
        RebuildCost.LOW: 10,
        RebuildCost.MEDIUM: 25,
        RebuildCost.HIGH: 40,
    }[rebuild_cost]
    return max(0, min(100, round(0.6 * size_score + 0.4 * idle_score - penalty)))


def _age_bucket(idle_days: float | None, buckets: tuple[int, ...]) -> str | None:
    if idle_days is None or not buckets:
        return None
    previous = 0
    for cutoff in buckets:
        if idle_days < cutoff:
            return f"{previous}-{cutoff}d"
        previous = cutoff
    return f">={buckets[-1]}d"


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


def _normalize(path: str | os.PathLike[str] | PureWindowsPath) -> str:
    rendered = str(PureWindowsPath(os.fspath(path)))
    return rendered.rstrip("\\/").casefold()


def _matches(path: str, candidate: str, kind: MatchKind) -> bool:
    candidate = candidate.casefold()
    if kind is MatchKind.EXACT:
        return path == candidate
    if kind is MatchKind.PREFIX:
        return path == candidate or path.startswith(candidate.rstrip("\\") + "\\")
    return fnmatch.fnmatchcase(path, candidate)


def _expand_braces(pattern: str) -> tuple[str, ...]:
    start = pattern.find("{")
    if start < 0:
        return (pattern,)
    end = pattern.find("}", start + 1)
    if end < 0:
        return (pattern,)
    choices = pattern[start + 1 : end].split(",")
    expanded: list[str] = []
    for choice in choices:
        expanded.extend(_expand_braces(pattern[:start] + choice + pattern[end + 1 :]))
    return tuple(expanded)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
    "CODEX_RULES",
    "DecisionOwner",
    "LastUseStrategy",
    "MatchKind",
    "PolicyAction",
    "RebuildCost",
    "application_process_running",
    "application_roots",
    "clear_process_cache",
    "effective_idle_days",
    "evaluate_application_path",
    "match_application_rule",
    "process_guard_allows",
]
