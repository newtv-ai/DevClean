"""Bounded SHA-256 duplicate detection for large ordinary files."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from devclean.core.triage import is_protected_path
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.scanner import CancellationToken, ScanOptions, ScanRecord, ScanRecordKind, scan_roots

_DEFAULT_MINIMUM_SIZE = 1 * 1024 * 1024
_DEFAULT_MAX_SIZE_GROUPS = 500
_DEFAULT_MAX_FILES_PER_SIZE_GROUP = 16
_HASH_CHUNK_SIZE = 1 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    digest: str
    logical_size: int
    records: tuple[ScanRecord, ...]

    @property
    def reclaimable_logical_bytes(self) -> int:
        return self.logical_size * max(0, len(self.records) - 1)


@dataclass(frozen=True, slots=True)
class DuplicateScanResult:
    groups: tuple[DuplicateGroup, ...]
    files_seen: int
    files_hashed: int
    truncated: bool
    cancelled: bool


def find_large_duplicates(
    roots: tuple[Path, ...],
    *,
    cancel: CancellationToken | None = None,
    minimum_size: int = _DEFAULT_MINIMUM_SIZE,
    max_size_groups: int = _DEFAULT_MAX_SIZE_GROUPS,
    max_files_per_size_group: int = _DEFAULT_MAX_FILES_PER_SIZE_GROUP,
) -> DuplicateScanResult:
    """Find byte-identical large files without a full-disk database.

    The first pass retains only the largest same-size groups.  The second pass
    hashes those exact regular files and reports only SHA-256 matches.  This is
    intentionally a bounded, high-space-impact duplicate finder, not a claim
    to enumerate every small duplicate on a disk.
    """

    if minimum_size < 1 or max_size_groups < 1 or max_files_per_size_group < 2:
        raise ValueError("duplicate scan bounds must be positive and allow a pair")
    token = cancel or CancellationToken()
    candidates: dict[int, list[ScanRecord]] = {}
    files_seen = 0
    truncated = False
    for record in scan_roots(roots, ScanOptions(include_directories=False), cancel=token):
        if token.is_cancelled():
            break
        if not _is_hash_candidate(record, minimum_size):
            continue
        files_seen += 1
        group = candidates.get(record.logical_size)
        if group is not None:
            if len(group) < max_files_per_size_group:
                group.append(record)
            else:
                truncated = True
            continue
        if len(candidates) < max_size_groups:
            candidates[record.logical_size] = [record]
            continue
        smallest = min(candidates)
        if record.logical_size > smallest:
            del candidates[smallest]
            candidates[record.logical_size] = [record]
        truncated = True

    groups: list[DuplicateGroup] = []
    files_hashed = 0
    for logical_size, records in sorted(candidates.items(), reverse=True):
        if token.is_cancelled():
            break
        if len(records) < 2:
            continue
        by_digest: dict[str, list[ScanRecord]] = {}
        for record in records:
            if token.is_cancelled():
                break
            digest = _hash_if_unchanged(record)
            if digest is None:
                continue
            files_hashed += 1
            by_digest.setdefault(digest, []).append(record)
        for digest, matching in by_digest.items():
            if len(matching) > 1:
                groups.append(
                    DuplicateGroup(
                        digest=digest,
                        logical_size=logical_size,
                        records=tuple(sorted(matching, key=lambda item: item.path.casefold())),
                    )
                )
    groups.sort(key=lambda group: group.reclaimable_logical_bytes, reverse=True)
    return DuplicateScanResult(
        groups=tuple(groups),
        files_seen=files_seen,
        files_hashed=files_hashed,
        truncated=truncated,
        cancelled=token.is_cancelled(),
    )


def _is_hash_candidate(record: ScanRecord, minimum_size: int) -> bool:
    return (
        record.kind is ScanRecordKind.FILE
        and record.logical_size >= minimum_size
        and record.link_count == 1
        and record.file_id_kind == "file_id_128"
        and record.volume_serial is not None
        and record.file_id is not None
        and record.creation_time_ns is not None
        and record.last_write_time_ns is not None
        and not is_protected_path(Path(record.path))
    )


def _hash_if_unchanged(record: ScanRecord) -> str | None:
    path = Path(record.path)
    try:
        metadata = read_file_metadata(path)
    except OSError:
        return None
    if (
        metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.volume_serial != record.volume_serial
        or metadata.file_id != record.file_id
        or metadata.file_id_kind != record.file_id_kind
        or metadata.link_count != 1
        or metadata.logical_size != record.logical_size
        or metadata.creation_time_ns != record.creation_time_ns
        or metadata.last_write_time_ns != record.last_write_time_ns
    ):
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


__all__ = ["DuplicateGroup", "DuplicateScanResult", "find_large_duplicates"]
