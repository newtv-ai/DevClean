"""Resolve recent-use signals for audited application cleanup rules.

Safety comes from the semantic cleanup profile. This module only answers the
separate utility question: when was the data or owning application last used?
It prefers application-owned timestamps where they exist and falls back to the
filesystem timestamp captured by the scanner.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    LastUseStrategy,
    application_roots,
)

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


def resolve_application_last_used(
    path: str | os.PathLike[str],
    rule: ApplicationCleanupRule,
    fallback: datetime | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> datetime | None:
    """Return the strongest available recent-use timestamp for one rule.

    ``fallback`` is normally the scanner's last-write timestamp. Codex itself
    uses rollout file modification time as thread recency, so session rollouts
    deliberately keep that signal. Aggregate ``history.jsonl`` records carry
    their own ``ts`` field, which is preferred over the file timestamp.
    """

    if rule.app_id != "codex":
        return fallback
    if rule.last_use is LastUseStrategy.JSONL_RECORD_TS:
        return _latest_codex_history_timestamp(Path(path)) or fallback
    if rule.last_use is LastUseStrategy.APP_ACTIVITY:
        root = _root_for_rule(rule, environment)
        if root is not None:
            return _codex_activity_cached(os.fspath(root)) or fallback
    return fallback


def clear_usage_cache() -> None:
    """Clear cached application-activity observations before a new scan."""

    _codex_activity_cached.cache_clear()


def _root_for_rule(
    rule: ApplicationCleanupRule,
    environment: Mapping[str, str] | None,
) -> Path | None:
    for root in application_roots(environment):
        if root.key == "CODEX_HOME":
            return Path(str(root.path))
    return None


@lru_cache(maxsize=16)
def _codex_activity_cached(codex_home: str) -> datetime | None:
    """Use mutable Codex state as a low-cost proxy for application activity."""

    root = Path(codex_home)
    latest_ns: int | None = None
    for name in _CODEX_ACTIVITY_FILES:
        try:
            observed = (root / name).stat().st_mtime_ns
        except OSError:
            continue
        latest_ns = observed if latest_ns is None else max(latest_ns, observed)
    if latest_ns is None:
        return None
    return datetime.fromtimestamp(latest_ns / 1_000_000_000, tz=UTC)


def _latest_codex_history_timestamp(path: Path) -> datetime | None:
    """Read only the tail of Codex input history and return its last valid ts."""

    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _HISTORY_TAIL_BYTES), os.SEEK_SET)
            data = handle.read()
    except OSError:
        return None

    lines = data.splitlines()
    # If the read began in the middle of a line, discard that partial record.
    if size > _HISTORY_TAIL_BYTES and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        try:
            payload = json.loads(raw)
            timestamp = payload.get("ts")
            if not isinstance(timestamp, (int, float)):
                continue
            return datetime.fromtimestamp(float(timestamp), tz=UTC)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, OverflowError, ValueError):
            continue
    return None


__all__ = [
    "clear_usage_cache",
    "resolve_application_last_used",
]
