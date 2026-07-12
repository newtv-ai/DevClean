"""Bounded category and top-directory summaries for a streaming GUI scan."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from reclaimer.core.cleanup_catalog import CleanupCategory

if TYPE_CHECKING:
    from reclaimer.core.triage import TriageItem

_MAX_DIRECTORY_BUCKETS = 2_000


@dataclass(slots=True)
class InsightSummary:
    files: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0


@dataclass(frozen=True, slots=True)
class DirectoryInsight:
    path: str
    summary: InsightSummary


class ScanInsights:
    """Exact category totals and bounded top-level directory totals.

    Directory buckets are intentionally limited.  Category totals always remain
    exact; if a root has more than 2,000 immediate children, the UI labels the
    directory view as partial instead of creating an unbounded in-memory index.
    """

    def __init__(self) -> None:
        self._categories = {category: InsightSummary() for category in CleanupCategory}
        self._directories: dict[str, InsightSummary] = {}
        self.skipped_directory_buckets = 0

    def add(self, item: TriageItem) -> None:
        _add(self._categories[item.category], item)
        bucket = _top_level_bucket(item)
        if bucket in self._directories:
            _add(self._directories[bucket], item)
        elif len(self._directories) < _MAX_DIRECTORY_BUCKETS:
            summary = InsightSummary()
            _add(summary, item)
            self._directories[bucket] = summary
        else:
            self.skipped_directory_buckets += 1

    def category_summary(self, category: CleanupCategory) -> InsightSummary:
        source = self._categories[category]
        return InsightSummary(source.files, source.logical_bytes, source.allocated_bytes)

    def category_items(self) -> tuple[tuple[CleanupCategory, InsightSummary], ...]:
        return tuple(
            (category, self.category_summary(category))
            for category in sorted(
                CleanupCategory,
                key=lambda value: self._categories[value].allocated_bytes,
                reverse=True,
            )
            if self._categories[category].files
        )

    def top_directories(self, *, limit: int = 100) -> tuple[DirectoryInsight, ...]:
        if limit < 1:
            raise ValueError("directory insight limit must be positive")
        ranked = sorted(
            self._directories.items(), key=lambda item: item[1].allocated_bytes, reverse=True
        )[:limit]
        return tuple(
            DirectoryInsight(
                path=path,
                summary=InsightSummary(
                    summary.files, summary.logical_bytes, summary.allocated_bytes
                ),
            )
            for path, summary in ranked
        )


def _add(summary: InsightSummary, item: TriageItem) -> None:
    summary.files += 1
    summary.logical_bytes += item.logical_size
    if item.allocated_size is not None:
        summary.allocated_bytes += item.allocated_size


def _top_level_bucket(item: TriageItem) -> str:
    root = Path(item.record.root)
    path = Path(item.path)
    try:
        relative = Path(os.path.relpath(path, root))
    except ValueError:
        return str(root)
    return str(root / relative.parts[0]) if relative.parts else str(root)


__all__ = ["DirectoryInsight", "InsightSummary", "ScanInsights"]
