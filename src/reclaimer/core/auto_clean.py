"""Narrow automatic cleanup for deterministic user temporary-file candidates."""

from __future__ import annotations

from pathlib import Path

from reclaimer.core.triage import requires_ai_review_path
from reclaimer.platform.windows.filesystem import FileSystemMetadata
from reclaimer.platform.windows.permanent_delete import permanently_delete_verified_file
from reclaimer.scanner.filesystem import ScanRecord, ScanRecordKind


def permanently_clean_temp_record(record: ScanRecord, *, temp_root: Path) -> None:
    """Permanently clean one already-triaged regular file from the approved temp root."""

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("automatic cleanup accepts file observations only")
    permanently_delete_verified_file(
        Path(record.path), approved_root=temp_root, expected=_snapshot_from_record(record)
    )


def permanently_clean_model_approved_record(record: ScanRecord) -> None:
    """Permanently clean one exact AI-approved file in a recognized cache location.

    The model cannot provide a path or expand a scope: it can only approve an
    existing in-memory scan record that passed this same cache-path classifier.
    """

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("model-approved cleanup accepts file observations only")
    source = Path(record.path)
    if not requires_ai_review_path(source):
        raise ValueError("model-approved cleanup accepts recognized cache files only")
    permanently_delete_verified_file(
        source, approved_root=source.parent, expected=_snapshot_from_record(record)
    )


def _snapshot_from_record(record: ScanRecord) -> FileSystemMetadata:
    return FileSystemMetadata(
        is_directory=False,
        logical_size=record.logical_size,
        allocation_size=record.raw_allocated_size,
        volume_serial=record.volume_serial,
        file_id=record.file_id,
        file_id_kind=record.file_id_kind,
        link_count=record.link_count,
        attributes=record.attributes,
        reparse_tag=record.reparse_tag,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=record.creation_time_ns,
        last_write_time_ns=record.last_write_time_ns,
    )


__all__ = [
    "permanently_clean_model_approved_record",
    "permanently_clean_temp_record",
]
