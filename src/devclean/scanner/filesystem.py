"""Streaming file-system inventory.

The scanner intentionally has no delete or elevation capability. A generic
tree scan produces evidence only, and its output cannot be imported as
execution authority.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Event
from typing import TypeAlias

from devclean.platform.windows import FileSystemMetadata, read_file_metadata

PathLike: TypeAlias = str | os.PathLike[str]


class ScanRecordKind(StrEnum):
    """The observational kind of a streamed scan record."""

    FILE = "file"
    DIRECTORY = "directory"
    BOUNDARY = "boundary"
    ERROR = "error"


class BoundaryReason(StrEnum):
    """Why traversal stopped at an otherwise visible object."""

    REPARSE_POINT = "reparse_point"
    CLOUD_FILES_PLACEHOLDER = "cloud_files_placeholder"


@dataclass(frozen=True, slots=True)
class ScanOptions:
    """Options that do not broaden the scanner beyond report-only behavior."""

    include_directories: bool = True
    deduplicate_hardlinks: bool = True
    progress_interval: int = 256
    max_hardlink_identities: int = 100_000

    def __post_init__(self) -> None:
        if self.progress_interval < 1:
            raise ValueError("progress_interval must be at least 1")
        if self.max_hardlink_identities < 1:
            raise ValueError("max_hardlink_identities must be at least 1")


@dataclass(frozen=True, slots=True)
class ScanRecord:
    """One streamed item of file-system evidence.

    For duplicate hard-link identities, ``raw_allocated_size`` keeps the
    per-name kernel value while ``allocated_size`` is zero.  This makes sums of
    ``allocated_size`` physical-allocation aware without discarding evidence.
    Boundary and directory records intentionally report zero sizes.
    """

    root: str
    path: str
    kind: ScanRecordKind
    depth: int
    logical_size: int = 0
    allocated_size: int | None = 0
    raw_allocated_size: int | None = 0
    volume_serial: int | None = None
    file_id: str | None = None
    file_id_kind: str | None = None
    link_count: int | None = None
    attributes: int | None = None
    reparse_tag: int | None = None
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None
    hardlink_duplicate: bool = False
    allocation_uncertain: bool = False
    boundary_reason: BoundaryReason | None = None
    error: str | None = None
    error_code: int | None = None


@dataclass(slots=True)
class ScanStats:
    """Cumulative counters supplied to progress callbacks as snapshots."""

    roots_seen: int = 0
    records: int = 0
    files: int = 0
    directories: int = 0
    boundaries: int = 0
    errors: int = 0
    inaccessible: int = 0
    hardlink_duplicates: int = 0
    allocation_unknown_files: int = 0
    hardlink_identity_capacity_reached: bool = False
    logical_bytes: int = 0
    allocated_bytes: int = 0
    raw_allocated_bytes: int = 0
    cancelled: bool = False
    completed: bool = False

    def snapshot(self) -> ScanStats:
        """Return an independent snapshot safe for asynchronous display."""

        return replace(self)


class CancellationToken:
    """Thread-safe cooperative cancellation for long scans."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        """Request cancellation; scanning stops at the next item boundary."""

        self._event.set()

    def is_cancelled(self) -> bool:
        """Return current state without implying it is stable across calls."""

        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""

        return self.is_cancelled()


ProgressCallback: TypeAlias = Callable[[ScanStats], None]


@dataclass(slots=True)
class _Frame:
    path: str
    depth: int
    iterator: Iterator[os.DirEntry[str]]
    close: Callable[[], None]


class _BoundedIdentityStore:
    """Bounded-memory hard-link identity tracker.

    Once capacity is reached, unseen multi-link files are reported with unknown
    accounted allocation instead of being over-counted.  Existing identities
    remain detectable.  This keeps memory bounded on million-file trees.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._identities: set[tuple[int, str]] = set()
        self.saturated = False

    def seen_or_add(self, identity: tuple[int, str]) -> bool | None:
        """Return seen/new, or ``None`` when a new identity cannot be stored."""

        if identity in self._identities:
            return True
        if len(self._identities) >= self._capacity:
            self.saturated = True
            return None
        self._identities.add(identity)
        return False


def _error_code(error: OSError) -> int | None:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return int(winerror)
    return error.errno


def _error_record(root: str, path: str, depth: int, error: OSError) -> ScanRecord:
    return ScanRecord(
        root=root,
        path=path,
        kind=ScanRecordKind.ERROR,
        depth=depth,
        allocated_size=None,
        raw_allocated_size=None,
        error=f"{type(error).__name__}: {error}",
        error_code=_error_code(error),
    )


def _metadata_kind(metadata: FileSystemMetadata) -> tuple[ScanRecordKind, BoundaryReason | None]:
    if metadata.is_cloud_placeholder:
        return (ScanRecordKind.BOUNDARY, BoundaryReason.CLOUD_FILES_PLACEHOLDER)
    if metadata.is_reparse_point:
        return (ScanRecordKind.BOUNDARY, BoundaryReason.REPARSE_POINT)
    if metadata.is_directory:
        return (ScanRecordKind.DIRECTORY, None)
    return (ScanRecordKind.FILE, None)


def _same_directory(before: FileSystemMetadata, after: FileSystemMetadata) -> bool:
    if not before.is_directory or not after.is_directory:
        return False
    if before.is_reparse_point or after.is_reparse_point:
        return False
    if before.is_cloud_placeholder or after.is_cloud_placeholder:
        return False
    if before.identity is not None and after.identity is not None:
        return before.identity == after.identity
    # Without a stable identity, fail closed rather than descend a directory
    # that may have been replaced between the two observations.
    return False


def _base_record(
    root: str,
    path: str,
    depth: int,
    metadata: FileSystemMetadata,
) -> ScanRecord:
    kind, boundary_reason = _metadata_kind(metadata)
    if kind is ScanRecordKind.FILE:
        logical_size = metadata.logical_size
        raw_allocated_size = metadata.allocation_size
    else:
        logical_size = 0
        raw_allocated_size = 0

    return ScanRecord(
        root=root,
        path=path,
        kind=kind,
        depth=depth,
        logical_size=logical_size,
        allocated_size=raw_allocated_size,
        raw_allocated_size=raw_allocated_size,
        volume_serial=metadata.volume_serial,
        file_id=metadata.file_id,
        file_id_kind=metadata.file_id_kind,
        link_count=metadata.link_count,
        attributes=metadata.attributes,
        reparse_tag=metadata.reparse_tag,
        creation_time_ns=metadata.creation_time_ns,
        last_write_time_ns=metadata.last_write_time_ns,
        boundary_reason=boundary_reason,
    )


def _prepare_path(
    root: str,
    path: str,
    depth: int,
    options: ScanOptions,
) -> tuple[list[ScanRecord], _Frame | None]:
    try:
        metadata = read_file_metadata(path)
    except OSError as error:
        return ([_error_record(root, path, depth, error)], None)

    record = _base_record(root, path, depth, metadata)
    if record.kind is not ScanRecordKind.DIRECTORY:
        return ([record], None)

    try:
        scandir_iterator = os.scandir(path)
    except OSError as error:
        records = [record] if options.include_directories else []
        records.append(_error_record(root, path, depth, error))
        return (records, None)

    # os.scandir binds its iterator to the opened directory.  Recheck identity
    # before yielding control to the caller, so a replacement between metadata
    # collection and iterator creation fails closed.
    try:
        confirmed = read_file_metadata(path)
    except OSError as error:
        scandir_iterator.close()
        return ([_error_record(root, path, depth, error)], None)

    if not _same_directory(metadata, confirmed):
        scandir_iterator.close()
        changed = OSError(errno.ESTALE, "directory identity changed during scan", path)
        return ([_error_record(root, path, depth, changed)], None)

    records = [_base_record(root, path, depth, confirmed)] if options.include_directories else []
    frame = _Frame(
        path=path,
        depth=depth,
        iterator=iter(scandir_iterator),
        close=scandir_iterator.close,
    )
    return (records, frame)


def _account_record(
    record: ScanRecord,
    options: ScanOptions,
    identity_store: _BoundedIdentityStore,
) -> ScanRecord:
    if record.kind is not ScanRecordKind.FILE or record.allocated_size is None:
        return record
    if not options.deduplicate_hardlinks:
        return record
    if record.link_count is None or record.link_count <= 1:
        return record
    if record.volume_serial is None or record.file_id is None:
        return replace(record, allocated_size=None, allocation_uncertain=True)

    status = identity_store.seen_or_add((record.volume_serial, record.file_id))
    if status is None:
        return replace(record, allocated_size=None, allocation_uncertain=True)
    if status:
        return replace(record, allocated_size=0, hardlink_duplicate=True)
    return record


def _update_stats(
    stats: ScanStats,
    record: ScanRecord,
    identity_store: _BoundedIdentityStore,
) -> None:
    stats.records += 1
    stats.hardlink_identity_capacity_reached = identity_store.saturated
    if record.kind is ScanRecordKind.FILE:
        stats.files += 1
        stats.logical_bytes += record.logical_size
        if record.allocated_size is not None:
            stats.allocated_bytes += record.allocated_size
        else:
            stats.allocation_unknown_files += 1
        if record.raw_allocated_size is not None:
            stats.raw_allocated_bytes += record.raw_allocated_size
        if record.hardlink_duplicate:
            stats.hardlink_duplicates += 1
    elif record.kind is ScanRecordKind.DIRECTORY:
        stats.directories += 1
    elif record.kind is ScanRecordKind.BOUNDARY:
        stats.boundaries += 1
    else:
        stats.errors += 1
        if record.error_code in {errno.EACCES, errno.EPERM, 5}:
            stats.inaccessible += 1
def scan_roots(
    roots: Iterable[PathLike],
    options: ScanOptions | None = None,
    cancel: CancellationToken | None = None,
    progress: ProgressCallback | None = None,
) -> Iterator[ScanRecord]:
    """Stream a fail-closed, non-mutating inventory for *roots*.

    Reparse points and Cloud Files placeholders are emitted as boundaries and
    never descended.  Access and enumeration failures become error records so a
    partial report cannot be mistaken for a complete scan.
    """

    active_options = options or ScanOptions()
    token = cancel or CancellationToken()
    stats = ScanStats()
    identity_store = _BoundedIdentityStore(active_options.max_hardlink_identities)
    frames: list[_Frame] = []
    finished_normally = False

    def emit(record: ScanRecord) -> ScanRecord:
        accounted = _account_record(record, active_options, identity_store)
        _update_stats(stats, accounted, identity_store)
        if progress is not None and stats.records % active_options.progress_interval == 0:
            progress(stats.snapshot())
        return accounted

    try:
        for raw_root in roots:
            if token.is_cancelled():
                stats.cancelled = True
                break

            root = os.path.abspath(os.fspath(raw_root))
            stats.roots_seen += 1
            records, root_frame = _prepare_path(root, root, 0, active_options)
            for record in records:
                yield emit(record)
            if root_frame is not None:
                frames.append(root_frame)

            while frames:
                if token.is_cancelled():
                    stats.cancelled = True
                    break

                frame = frames[-1]
                try:
                    entry = next(frame.iterator)
                except StopIteration:
                    frame.close()
                    frames.pop()
                    continue
                except OSError as error:
                    frame.close()
                    frames.pop()
                    yield emit(_error_record(root, frame.path, frame.depth, error))
                    continue

                records, child_frame = _prepare_path(
                    root,
                    entry.path,
                    frame.depth + 1,
                    active_options,
                )
                for record in records:
                    yield emit(record)
                if child_frame is not None:
                    frames.append(child_frame)

            if stats.cancelled:
                break

        finished_normally = not stats.cancelled
    finally:
        while frames:
            frames.pop().close()
        stats.completed = finished_normally
        if progress is not None:
            progress(stats.snapshot())


__all__ = [
    "BoundaryReason",
    "CancellationToken",
    "ProgressCallback",
    "ScanOptions",
    "ScanRecord",
    "ScanRecordKind",
    "ScanStats",
    "scan_roots",
]
