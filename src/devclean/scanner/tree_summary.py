"""Fast aggregate metadata scan for rule-covered whole-tree caches.

This intentionally does less work than the generic scanner: it does not build a
ScanRecord for every file, run per-file classification, read file identities, or
retain child paths. It only gathers the evidence required by an already-known
whole-directory rule: file count, logical bytes, and newest observed timestamp.

An entry inside the tree that cannot be read is left out of those totals and
counted, and the rest of the tree is still summed (user decision 2026-11-11):
one locked or vanished-and-replaced folder no longer hides a whole cache from
the user.  Only the tree's own folder failing to read stops the summary.
"""

from __future__ import annotations

import errno
import os
import stat as stat_module
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from devclean.scanner.filesystem import CancellationToken

_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_EXAMPLES_KEPT = 20
_GONE_CODES = frozenset({2, 3})  # Windows: file / path not found
_IN_USE_CODES = frozenset({32, 33})  # Windows: sharing / lock violation
_DENIED_CODES = frozenset({5})  # Windows: access denied


class TreeSummaryIncomplete(OSError):
    """The tree's own folder could not be summarized (or the scan was cancelled)."""


class SkipReason(StrEnum):
    """Why one entry inside a summarized tree was left out of its totals."""

    IN_USE = "IN_USE"  # another program holds it
    DENIED = "DENIED"  # this account may not read it
    CHANGED = "CHANGED"  # it turned into a link or another object while being read
    UNREADABLE = "UNREADABLE"  # any other read error


@dataclass(frozen=True, slots=True)
class TreeSummary:
    files: int
    logical_bytes: int
    latest_activity_time_ns: int
    # Entries inside the tree that could not be read: left out of the totals,
    # while the rest of the tree is still counted.  How many, per reason, and a
    # few of them for the user to see.
    skipped: int = 0
    skipped_by_reason: tuple[tuple[SkipReason, int], ...] = ()
    skipped_examples: tuple[tuple[str, SkipReason], ...] = ()


def _is_gone(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _GONE_CODES
    return error.errno == errno.ENOENT


def _reason(error: OSError) -> SkipReason:
    winerror = getattr(error, "winerror", None)
    code = winerror if winerror is not None else error.errno
    if code in _IN_USE_CODES or (winerror is None and code == errno.EBUSY):
        return SkipReason.IN_USE
    if (
        isinstance(error, PermissionError)
        or code in _DENIED_CODES
        or (winerror is None and code in {errno.EACCES, errno.EPERM})
    ):
        return SkipReason.DENIED
    if winerror is None and code in {errno.ELOOP, errno.ENOTDIR, errno.ESTALE}:
        return SkipReason.CHANGED
    return SkipReason.UNREADABLE


def _still_the_listed_folder(path: str, listed_inode: int) -> SkipReason | None:
    """Why *path*, just opened for listing, is no longer the folder that was listed.

    ``os.scandir`` opens a path and follows a link found there, so a folder
    swapped for a link (or a junction) after its parent was listed would lead
    the summary out of the tree.  Re-reading the name itself, without
    following it, after the listing is open catches that; the folder is then
    skipped instead.  *listed_inode* is the identity seen in the parent's
    listing where the platform reports one for free (0 on Windows).
    """

    try:
        now = os.lstat(path)
    except OSError as error:
        return None if _is_gone(error) else _reason(error)
    if (
        not stat_module.S_ISDIR(now.st_mode)
        or getattr(now, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        or (listed_inode and now.st_ino != listed_inode)
    ):
        return SkipReason.CHANGED
    return None


def summarize_tree(path: Path, cancel: CancellationToken | None = None) -> TreeSummary:
    """Summarize one directory without materializing or classifying its children.

    Links and reparse points are never followed or counted.  An entry inside
    the tree that cannot be read -- held by another program, not readable by
    this account, or turned into a link or another object while it was read --
    is skipped and counted (``skipped``); an entry that simply vanished is not
    counted.  Only the tree's own folder failing to read raises
    :class:`TreeSummaryIncomplete`, as does cancelling.
    """

    root = os.path.abspath(path)
    try:
        root_stat = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise TreeSummaryIncomplete(str(error)) from error

    latest_ns = max(root_stat.st_ctime_ns, root_stat.st_mtime_ns)
    files = 0
    logical_bytes = 0
    skipped: dict[SkipReason, int] = {}
    examples: list[tuple[str, SkipReason]] = []

    def skip(where: str, reason: SkipReason) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1
        if len(examples) < _EXAMPLES_KEPT:
            examples.append((where, reason))

    # (folder, identity seen when its parent was listed; 0 when unknown)
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        if cancel is not None and cancel.is_cancelled():
            raise TreeSummaryIncomplete("scan cancelled")
        directory, listed_inode = stack.pop()
        try:
            listing = os.scandir(directory)
        except OSError as error:
            if directory == root:
                raise TreeSummaryIncomplete(str(error)) from error
            if not _is_gone(error):
                skip(directory, _reason(error))
            continue
        with listing as entries:
            if directory != root:
                changed = _still_the_listed_folder(directory, listed_inode)
                if changed is not None:
                    skip(directory, changed)
                    continue
            try:
                for entry in entries:
                    if cancel is not None and cancel.is_cancelled():
                        raise TreeSummaryIncomplete("scan cancelled")
                    try:
                        stat = entry.stat(follow_symlinks=False)
                        is_directory = entry.is_dir(follow_symlinks=False)
                        is_file = not is_directory and entry.is_file(follow_symlinks=False)
                    except OSError as error:
                        if not _is_gone(error):
                            skip(entry.path, _reason(error))
                        continue
                    latest_ns = max(latest_ns, stat.st_ctime_ns, stat.st_mtime_ns)
                    attributes = getattr(stat, "st_file_attributes", 0)
                    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        continue
                    if is_directory:
                        stack.append((entry.path, stat.st_ino))
                    elif is_file:
                        files += 1
                        logical_bytes += stat.st_size
            except TreeSummaryIncomplete:
                raise
            except OSError as error:
                # The listing broke off part-way: what it already gave is kept.
                if directory == root:
                    raise TreeSummaryIncomplete(str(error)) from error
                if not _is_gone(error):
                    skip(directory, _reason(error))

    return TreeSummary(
        files,
        logical_bytes,
        latest_ns,
        skipped=sum(skipped.values()),
        skipped_by_reason=tuple(sorted(skipped.items())),
        skipped_examples=tuple(examples),
    )


__all__ = ["SkipReason", "TreeSummary", "TreeSummaryIncomplete", "summarize_tree"]
