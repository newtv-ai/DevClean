"""Bounded in-memory triage for the scan → auto/AI/user cleanup workflow."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from reclaimer.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    known_root_for_path,
)
from reclaimer.core.scan_insights import ScanInsights
from reclaimer.scanner.filesystem import ScanRecord, ScanRecordKind

_DISPLAY_LIMIT = 500
_AUTO_TEMP_AGE = timedelta(days=7)
_PROTECTED_SEGMENTS = frozenset(
    {
        ".git",
        ".codex",
        ".claude",
        "globalstorage",
        "local history",
    }
)
_AI_CACHE_SEGMENTS = frozenset(
    {
        "huggingface",
        "pip",
        "uv",
        "npm-cache",
        "pnpm-store",
        "yarn",
        "gradle",
    }
)
_PROTECTED_SUFFIXES = frozenset({".key", ".pem", ".pfx", ".p12", ".kdbx"})


class ReviewLane(StrEnum):
    AUTO_CLEAN = "AUTO_CLEAN"
    AI_REVIEW = "AI_REVIEW"
    USER_REVIEW = "USER_REVIEW"
    PROTECTED = "PROTECTED"


@dataclass(frozen=True, slots=True)
class TriageItem:
    record: ScanRecord
    path: str
    logical_size: int
    allocated_size: int | None
    category: CleanupCategory
    lane: ReviewLane
    reason: str


@dataclass(slots=True)
class TriageSummary:
    files: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0


class TriageSession:
    """Keep aggregate scan totals plus a bounded display/review sample per lane."""

    def __init__(self, *, display_limit: int = _DISPLAY_LIMIT) -> None:
        if display_limit < 1:
            raise ValueError("display_limit must be positive")
        self._display_limit = display_limit
        self._summaries = {lane: TriageSummary() for lane in ReviewLane}
        self._items: dict[ReviewLane, list[TriageItem]] = {
            lane: [] for lane in ReviewLane
        }
        self._insights = ScanInsights()

    def add(self, item: TriageItem) -> None:
        summary = self._summaries[item.lane]
        summary.files += 1
        summary.logical_bytes += item.logical_size
        if item.allocated_size is not None:
            summary.allocated_bytes += item.allocated_size
        self._insights.add(item)

        displayed = self._items[item.lane]
        if len(displayed) < self._display_limit:
            displayed.append(item)
            return
        smallest = min(displayed, key=lambda existing: existing.logical_size)
        if item.logical_size > smallest.logical_size:
            displayed.remove(smallest)
            displayed.append(item)

    def summary(self, lane: ReviewLane) -> TriageSummary:
        source = self._summaries[lane]
        return TriageSummary(source.files, source.logical_bytes, source.allocated_bytes)

    def items(self, lane: ReviewLane) -> tuple[TriageItem, ...]:
        return tuple(sorted(self._items[lane], key=lambda item: item.logical_size, reverse=True))

    @property
    def insights(self) -> ScanInsights:
        return self._insights


def triage_file(
    record: ScanRecord,
    *,
    now: datetime | None = None,
    temp_root: Path | None = None,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> TriageItem:
    """Classify one observed regular file without reading its contents or persisting it."""

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("triage accepts file observations only")
    path = Path(record.path)
    category, lane, reason = _classify(
        path,
        record.last_write_time_ns,
        now=now,
        temp_root=temp_root,
        known_roots=known_roots,
    )
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=record.logical_size,
        allocated_size=record.allocated_size,
        category=category,
        lane=lane,
        reason=reason,
    )


def _classify(
    path: Path,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    temp_root: Path | None,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> tuple[CleanupCategory, ReviewLane, str]:
    if is_protected_path(path):
        return (
            CleanupCategory.OTHER,
            ReviewLane.PROTECTED,
            "Protected developer, credential, or editor-history asset",
        )
    known = known_root_for_path(path, known_roots)
    if known is not None:
        if known.policy is CleanupPolicy.AUTO_AFTER_AGE:
            if _is_older_than(last_write_time_ns, _AUTO_TEMP_AGE, now):
                return (
                    known.category,
                    ReviewLane.AUTO_CLEAN,
                    f"{known.label} older than seven days",
                )
            return (
                known.category,
                ReviewLane.USER_REVIEW,
                f"{known.label} is newer than the automatic-clean threshold",
            )
        return (
            known.category,
            ReviewLane.AI_REVIEW,
            f"{known.label} requires item-level explanation",
        )
    root = temp_root or Path(tempfile.gettempdir())
    if _is_descendant(path, root) and _is_older_than(last_write_time_ns, _AUTO_TEMP_AGE, now):
        return (
            CleanupCategory.USER_TEMP,
            ReviewLane.AUTO_CLEAN,
            "User temporary file older than seven days",
        )
    if requires_ai_review_path(path):
        return (
            CleanupCategory.OTHER,
            ReviewLane.AI_REVIEW,
            "Known development cache requires item-level explanation",
        )
    return (
        CleanupCategory.OTHER,
        ReviewLane.USER_REVIEW,
        "No deterministic safe-clean rule matches this file",
    )


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root)))
        ) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def is_protected_path(path: Path) -> bool:
    """Return whether a hard protection rule forbids all cleanup paths."""

    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(
        parts & _PROTECTED_SEGMENTS
        or name == ".env"
        or name.startswith(".env.")
        or path.suffix.casefold() in _PROTECTED_SUFFIXES
    )


def requires_ai_review_path(path: Path) -> bool:
    """Return whether an ordinary path is a recognized development-cache candidate."""

    return not is_protected_path(path) and bool(
        {part.casefold() for part in path.parts} & _AI_CACHE_SEGMENTS
    )


def _is_older_than(
    last_write_time_ns: int | None, age: timedelta, now: datetime | None
) -> bool:
    if last_write_time_ns is None:
        return False
    observed = datetime.fromtimestamp(last_write_time_ns / 1_000_000_000, tz=UTC)
    return observed <= (now or datetime.now(UTC)) - age


__all__ = [
    "CleanupCategory",
    "ReviewLane",
    "TriageItem",
    "TriageSession",
    "TriageSummary",
    "is_protected_path",
    "requires_ai_review_path",
    "triage_file",
]
