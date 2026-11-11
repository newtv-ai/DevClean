"""Application-aware cleanup policy with usage and reclaim-benefit semantics.

Application semantics outrank generic filename heuristics. A profile states
whether data is regenerable, user-owned, or persistent, then adds idle time,
rebuild cost, process guards, and minimum reclaim value. Age therefore affects
whether cleanup is useful, not whether deletion is intrinsically safe.

Codex is the first audited profile. Other developer tools should reuse this
policy model instead of adding one-off filename rules.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache, wraps
from pathlib import Path, PureWindowsPath
from typing import Protocol, cast

_MIB = 1024**2
_GIB = 1024**3
_HISTORY_TAIL_BYTES = 128 * 1024
_CODEX_ACTIVITY_FILES = (
    "state_5.sqlite",
    "state_5.sqlite-wal",
    "logs_2.sqlite",
    "logs_2.sqlite-wal",
    "history.jsonl",
    "session_index.jsonl",
    "goals_1.sqlite",
    "memories_1.sqlite",
)


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


class RootBuilder[T](Protocol):
    def __call__(self, environment: Mapping[str, str] | None = None) -> T: ...


# A scan classifies every file against every application profile, and each
# profile rebuilds its roots from the environment, registry and vendor files.
# Inside one scan those inputs are fixed, so each builder runs once per scan.
# Execution-time checks run outside a scan and always rediscover.
_scan_roots: ContextVar[dict[object, object] | None] = ContextVar("_scan_roots", default=None)


@contextmanager
def scan_root_snapshot() -> Iterator[None]:
    """Reuse application root discovery for the duration of one scan."""

    token = _scan_roots.set({})
    try:
        yield
    finally:
        _scan_roots.reset(token)


def in_scan_snapshot() -> bool:
    """Whether root discovery is pinned for the current scan (see above)."""

    return _scan_roots.get() is not None


def snapshot_roots[T](builder: RootBuilder[T]) -> RootBuilder[T]:
    """Let *builder* reuse its live-environment result inside a scan snapshot."""

    @wraps(builder)
    def snapshotted(environment: Mapping[str, str] | None = None) -> T:
        snapshot = _scan_roots.get()
        if snapshot is None or environment is not None:
            return builder(environment)
        if builder not in snapshot:
            snapshot[builder] = builder(None)
        return cast(T, snapshot[builder])

    return snapshotted


# Answers shared by every caller in one scan or cleanup run (see run_cached).
_run_cache: dict[object, object] = {}


def run_cached[T](key: object, build: Callable[[], T]) -> T:
    """Share *build*'s answer across one scan or cleanup run.

    Inside :func:`scan_root_snapshot` the answer lasts exactly that run, so a
    cleanup sees the state at its own start rather than the scan's. Outside a
    snapshot it is kept until :func:`clear_process_cache`.
    """

    snapshot = _scan_roots.get()
    store = snapshot if snapshot is not None else _run_cache
    marker = ("run_cached", key)
    if marker not in store:
        store[marker] = build()
    return cast(T, store[marker])


def forget_run_cached(*keys: object) -> None:
    """Drop answers kept outside a run; a run keeps its own until it ends."""

    for key in keys:
        _run_cache.pop(("run_cached", key), None)


def first_in_run(key: object) -> bool:
    """True the first time *key* is seen in the current snapshot; always True outside one."""

    snapshot = _scan_roots.get()
    if snapshot is None:
        return True
    marker = ("first_in_run", key)
    if marker in snapshot:
        return False
    snapshot[marker] = True
    return True


class ConfigCachedQuery[T]:
    """A tool query reused until that tool's own configuration files change.

    Package managers report where their caches live only when asked, which
    starts the tool. Asking again for every deletion would start every package
    manager for every file; never asking again would miss a location changed
    after the scan. The answer is therefore reused only while the environment
    and the stat signature of the tool's configuration files are unchanged.
    """

    def __init__(
        self,
        query: Callable[[], T],
        config_files: Callable[[], Iterable[str]],
    ) -> None:
        self._query = query
        self._config_files = config_files
        self._lock = threading.Lock()
        self._answer: tuple[tuple[object, ...], T] | None = None
        self.__doc__ = query.__doc__

    def __call__(self) -> T:
        signature = _config_signature(self._config_files())
        with self._lock:
            answer = self._answer
        if answer is not None and answer[0] == signature:
            return answer[1]
        value = self._query()
        with self._lock:
            self._answer = (signature, value)
        return value

    def cache_clear(self) -> None:
        with self._lock:
            self._answer = None


def cached_until_config_changes[T](
    config_files: Callable[[], Iterable[str]],
) -> Callable[[Callable[[], T]], ConfigCachedQuery[T]]:
    """Decorate a zero-argument tool query; see :class:`ConfigCachedQuery`."""

    def decorate(query: Callable[[], T]) -> ConfigCachedQuery[T]:
        return ConfigCachedQuery(query, config_files)

    return decorate


def _config_signature(paths: Iterable[str]) -> tuple[object, ...]:
    signature: list[object] = [hash(frozenset(os.environ.items()))]
    for path in paths:
        try:
            status = os.stat(path)
        except OSError:
            signature.append((path, None))
        else:
            signature.append((path, status.st_size, status.st_mtime_ns))
    return tuple(signature)


def config_paths(*candidates: str | None) -> tuple[str, ...]:
    """Drop unset candidates; a missing file still counts, so creating it is noticed."""

    return tuple(candidate for candidate in candidates if candidate)


def file_time_utc(value_ns: int | None) -> datetime | None:
    """Convert a file time, or ``None`` when Python cannot represent it here.

    NTFS stores times from 1601 to 30828. Python rejects some of those -- on
    Windows anything before 1970 -- with OSError, OverflowError or ValueError.
    One such file must not stop a scan; its age is simply unknown.
    """

    if value_ns is None:
        return None
    try:
        return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ApplicationRoot:
    key: str
    path: PureWindowsPath


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
        app_id="codex",
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


_STATE_DATABASES = (
    "{state_5.sqlite*,goals_1.sqlite*,memories_1.sqlite*,"
    "queue_1.sqlite*,thread_history_1.sqlite*}"
)
_DESKTOP_BROWSER_CACHES = (
    "{Cache,Code Cache,GPUCache,GrShaderCache,ShaderCache,DawnCache,"
    "DawnWebGPUCache,GraphiteDawnCache,component_crx_cache,extensions_crx_cache}"
)
_DESKTOP_DEFAULT_CACHES = (
    r"Default\{Cache,Code Cache,GPUCache,GrShaderCache,ShaderCache,DawnCache,"
    r"DawnWebGPUCache,GraphiteDawnCache}"
)
_DESKTOP_PROFILE_STATE = (
    r"{Local State,Default\Preferences,Default\Cookies*,Default\Login Data*,"
    r"Default\Local Storage,Default\Session Storage,Default\IndexedDB}"
)

# A directory named "cache" is not automatically disposable. In particular,
# Codex ``plugins/cache`` is the active installed-plugin store and is protected.
CODEX_RULES: tuple[ApplicationCleanupRule, ...] = (
    _rule(
        "codex-model-catalog",
        "CODEX_HOME",
        "models_cache.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Codex model catalog cache",
        idle_days=7,
        min_reclaim_bytes=_MIB,
    ),
    _rule(
        "codex-cloud-config-cache",
        "CODEX_HOME",
        "cloud-config-bundle-cache.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Codex cloud configuration cache",
        idle_days=7,
        min_reclaim_bytes=_MIB,
    ),
    _rule(
        "codex-version-cache",
        "CODEX_HOME",
        "version.json",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Codex update-check cache",
        idle_days=7,
        min_reclaim_bytes=_MIB,
    ),
    _rule(
        "codex-plugin-marketplace-snapshot",
        "CODEX_HOME",
        r".tmp\plugins",
        MatchKind.PREFIX,
        DecisionOwner.TOOL,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.MEDIUM,
        "Codex curated plugin marketplace snapshot",
        idle_days=30,
        min_reclaim_bytes=16 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "codex-plugin-sync-marker",
        "CODEX_HOME",
        r".tmp\plugins.sha",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex plugin sync marker",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "codex-plugin-sync-lock",
        "CODEX_HOME",
        r".tmp\plugins.sync.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex plugin synchronization lock",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "codex-rollout-maintenance-lock",
        "CODEX_HOME",
        r".tmp\rollout-maintenance.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex rollout maintenance lock",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "codex-rollout-compression-lock",
        "CODEX_HOME",
        r".tmp\rollout-compression.lock",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex rollout compression lock",
        idle_days=0.25,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "codex-orphan-rollout-temp",
        "CODEX_HOME",
        r".tmp\*.tmp",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex orphan rollout temporary file",
        idle_days=1,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "codex-log-db",
        "CODEX_HOME",
        "logs_2.sqlite*",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex tracing log database",
        idle_days=10,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
    ),
    _rule(
        "codex-active-sessions",
        "CODEX_HOME",
        "sessions",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.SESSION_LAST_EVENT,
        RebuildCost.HIGH,
        "Codex conversation history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "codex-archived-sessions",
        "CODEX_HOME",
        "archived_sessions",
        MatchKind.PREFIX,
        DecisionOwner.USER,
        LastUseStrategy.SESSION_LAST_EVENT,
        RebuildCost.HIGH,
        "Codex archived conversation history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "codex-input-history",
        "CODEX_HOME",
        "history.jsonl",
        MatchKind.EXACT,
        DecisionOwner.USER,
        LastUseStrategy.JSONL_RECORD_TS,
        RebuildCost.HIGH,
        "Codex input history",
        user_age_buckets=(30, 90, 180),
    ),
    _rule(
        "codex-installed-plugin-store",
        "CODEX_HOME",
        r"plugins\cache",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex installed plugin store",
    ),
    _rule(
        "codex-plugin-data",
        "CODEX_HOME",
        r"plugins\data",
        MatchKind.PREFIX,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex persistent plugin data",
    ),
    _rule(
        "codex-state-databases",
        "CODEX_HOME",
        _STATE_DATABASES,
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex persistent state databases",
    ),
    _rule(
        "codex-session-index",
        "CODEX_HOME",
        "session_index.jsonl",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex session index",
    ),
    _rule(
        "codex-auth",
        "CODEX_HOME",
        "auth.json",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex authentication state",
    ),
    _rule(
        "codex-config",
        "CODEX_HOME",
        "config.toml",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex configuration",
    ),
    _rule(
        "codex-desktop-crashpad-reports",
        "CODEX_DESKTOP",
        r"Crashpad\{reports,pending}",
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.NONE,
        "Codex desktop crash reports",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
    ),
    _rule(
        "codex-desktop-browser-cache",
        "CODEX_DESKTOP",
        _DESKTOP_BROWSER_CACHES,
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Codex desktop regenerable browser cache",
        idle_days=30,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "codex-desktop-default-browser-cache",
        "CODEX_DESKTOP",
        _DESKTOP_DEFAULT_CACHES,
        MatchKind.GLOB,
        DecisionOwner.TOOL,
        LastUseStrategy.FILE_MTIME,
        RebuildCost.LOW,
        "Codex desktop profile cache",
        idle_days=30,
        min_reclaim_bytes=8 * _MIB,
        requires_process_closed=True,
        allow_whole_tree=True,
    ),
    _rule(
        "codex-desktop-profile-state",
        "CODEX_DESKTOP",
        _DESKTOP_PROFILE_STATE,
        MatchKind.GLOB,
        DecisionOwner.KEEP,
        LastUseStrategy.APP_ACTIVITY,
        RebuildCost.HIGH,
        "Codex desktop browser profile state",
    ),
)


@snapshot_roots
def application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
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
    """Classify one path using semantic safety plus cleanup benefit."""

    rule = match_application_rule(path, environment)
    if rule is None:
        return None
    current = now or datetime.now(UTC)
    observed = _as_utc(
        _resolve_last_used(path, rule, last_used, environment=environment)
    )
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


def effective_idle_days(
    rule: ApplicationCleanupRule,
    logical_size: int,
) -> float | None:
    """Shorten an idle threshold only when reclaim value is materially large."""

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


def _resolve_last_used(
    path: str | os.PathLike[str],
    rule: ApplicationCleanupRule,
    fallback: datetime | None,
    *,
    environment: Mapping[str, str] | None,
) -> datetime | None:
    if rule.app_id != "codex":
        return fallback
    if rule.last_use is LastUseStrategy.JSONL_RECORD_TS:
        return _latest_codex_history_timestamp(Path(path)) or fallback
    if rule.last_use is LastUseStrategy.APP_ACTIVITY:
        root = next(
            (
                application_root.path
                for application_root in application_roots(environment)
                if application_root.key == "CODEX_HOME"
            ),
            None,
        )
        if root is not None:
            return _codex_activity_cached(str(root)) or fallback
    # Codex itself uses rollout file modification time as thread recency, so
    # SESSION_LAST_EVENT intentionally resolves to the scanner's file mtime.
    return fallback


@lru_cache(maxsize=16)
def _codex_activity_cached(codex_home: str) -> datetime | None:
    root = Path(codex_home)
    latest_ns: int | None = None
    for name in _CODEX_ACTIVITY_FILES:
        try:
            observed = (root / name).stat().st_mtime_ns
        except OSError:
            continue
        latest_ns = observed if latest_ns is None else max(latest_ns, observed)
    return file_time_utc(latest_ns)


def _latest_codex_history_timestamp(path: Path) -> datetime | None:
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        timestamp = payload.get("ts")
        if not isinstance(timestamp, (int, float)):
            continue
        try:
            return datetime.fromtimestamp(float(timestamp), tz=UTC)
        except (OSError, OverflowError, ValueError):
            continue
    return None


@lru_cache(maxsize=8)
def application_process_running(app_id: str) -> bool:
    """Best-effort process guard used during one scan/session."""

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
        # Unknown process state must not widen deletion authority.
        return True
    if result.returncode != 0:
        return True
    try:
        rows = list(csv.reader(result.stdout.splitlines(), strict=True))
    except csv.Error:
        return True
    if not rows or any(len(row) != 5 or not row[1].isdigit() for row in rows):
        return True
    return any(row[0].casefold() in {"codex.exe", "chatgpt.exe"} for row in rows)


def clear_process_cache() -> None:
    application_process_running.cache_clear()
    _codex_activity_cached.cache_clear()
    _run_cache.clear()


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
    return _normalize_text(os.fspath(path))


# Pure string work. Every scanned file is compared with the same few hundred
# audited roots, so caching avoids re-parsing those roots for each file.
@lru_cache(maxsize=4096)
def _normalize_text(path: str) -> str:
    rendered = str(PureWindowsPath(path))
    return rendered.rstrip("\\/").casefold()


# Rule candidates and roots are a few hundred fixed strings, compared with every
# scanned file and every entry of a temp folder being emptied: their folded
# form, "below me" prefix and wildcard flag are worked out once, not per call.
@lru_cache(maxsize=8192)
def _candidate_form(candidate: str) -> tuple[str, str, bool]:
    folded = candidate.casefold()
    return folded, folded.rstrip("\\") + "\\", any(char in folded for char in "*?[")


def _matches(path: str, candidate: str, kind: MatchKind) -> bool:
    folded, below, wildcard = _candidate_form(candidate)
    if kind is MatchKind.EXACT:
        return path == folded
    if kind is MatchKind.PREFIX:
        return path == folded or path.startswith(below)
    if fnmatch.fnmatchcase(path, folded):
        return True
    if not wildcard:
        return path.startswith(below)
    return False


def _could_hold(normalized_root: str, normalized_path: str) -> bool:
    """Whether a rule anchored at *normalized_root* could match *normalized_path*.

    Every candidate is the root itself or the root, a backslash and a relative
    pattern, so :func:`_matches` can only succeed for the root or a path below
    it -- unless the root itself contains wildcard characters.
    """

    below, wildcard = _root_form(normalized_root)
    return normalized_path == normalized_root or normalized_path.startswith(below) or wildcard


@lru_cache(maxsize=4096)
def _root_form(normalized_root: str) -> tuple[str, bool]:
    return normalized_root.rstrip("\\") + "\\", any(char in normalized_root for char in "*?[")


@lru_cache(maxsize=4096)
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
    "CODEX_RULES",
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
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
