"""Fast aggregate metadata scan for rule-covered whole-tree caches.

This intentionally does less work than the generic scanner: it does not build a
ScanRecord for every file, run per-file classification, read file identities, or
retain child paths. It only gathers the evidence required by an already-known
whole-directory rule: file count, logical bytes, and newest observed timestamp.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from devclean.scanner.filesystem import CancellationToken

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class TreeSummaryIncomplete(OSError):
    """A metadata-only summary could not cover the complete tree safely."""


@dataclass(frozen=True, slots=True)
class TreeSummary:
    files: int
    logical_bytes: int
    latest_activity_time_ns: int


def summarize_tree(path: Path, cancel: CancellationToken | None = None) -> TreeSummary:
    """Summarize one directory without materializing or classifying its children."""

    root = os.path.abspath(path)
    try:
        root_stat = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise TreeSummaryIncomplete(str(error)) from error

    latest_ns = max(root_stat.st_ctime_ns, root_stat.st_mtime_ns)
    files = 0
    logical_bytes = 0
    stack = [root]

    while stack:
        if cancel is not None and cancel.is_cancelled():
            raise TreeSummaryIncomplete("scan cancelled")
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancel is not None and cancel.is_cancelled():
                        raise TreeSummaryIncomplete("scan cancelled")
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise TreeSummaryIncomplete(str(error)) from error
                    latest_ns = max(latest_ns, stat.st_ctime_ns, stat.st_mtime_ns)
                    attributes = getattr(stat, "st_file_attributes", 0)
                    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            logical_bytes += stat.st_size
                    except OSError as error:
                        raise TreeSummaryIncomplete(str(error)) from error
        except TreeSummaryIncomplete:
            raise
        except OSError as error:
            raise TreeSummaryIncomplete(str(error)) from error

    return TreeSummary(files, logical_bytes, latest_ns)


__all__ = ["TreeSummary", "TreeSummaryIncomplete", "summarize_tree"]
