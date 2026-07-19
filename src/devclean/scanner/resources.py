"""Map observational scan records into non-actionable domain resources."""

from __future__ import annotations

from devclean.core.models import (
    Confidence,
    FileIdentity,
    ProvenanceClass,
    Resource,
    RiskTier,
    SemanticType,
    SizeValue,
    new_id,
)
from devclean.scanner.filesystem import BoundaryReason, ScanRecord, ScanRecordKind


def file_record_to_resource(record: ScanRecord) -> Resource:
    """Convert a file observation without granting it execution authority."""

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("only file records can become filesystem resources")

    allocated = (
        SizeValue(record.raw_allocated_size, Confidence.EXACT)
        if record.raw_allocated_size is not None
        else SizeValue(None, Confidence.UNKNOWN)
    )
    warnings: list[str] = [
        "Scan observation only; it cannot be imported or used as execution authority."
    ]
    if record.hardlink_duplicate:
        warnings.append(
            "This is a duplicate hard-link name; aggregate allocation counts its shared bytes "
            "only once."
        )
    if record.allocation_uncertain:
        warnings.append(
            "Allocated bytes are unknown because hard-link identity accounting was unavailable "
            "or reached its configured bound."
        )

    identity = None
    if any(
        value is not None
        for value in (
            record.volume_serial,
            record.file_id,
            record.link_count,
            record.attributes,
            record.reparse_tag,
            record.creation_time_ns,
            record.last_write_time_ns,
        )
    ):
        identity = FileIdentity(
            volume_serial=(
                f"{record.volume_serial:016x}"
                if record.volume_serial is not None
                else None
            ),
            file_id=record.file_id,
            file_id_kind=record.file_id_kind,
            link_count=record.link_count,
            attributes=record.attributes,
            reparse_tag=record.reparse_tag,
            creation_time_ns=record.creation_time_ns,
            last_write_time_ns=record.last_write_time_ns,
        )

    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Filesystem file",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=record.path,
        logical_size=SizeValue(record.logical_size, Confidence.EXACT),
        allocated_size=allocated,
        identity=identity,
        warnings=tuple(warnings),
        actionable=False,
    )


def record_to_scan_error(record: ScanRecord) -> tuple[str, str, str] | None:
    """Convert an error or traversal boundary into a durable report entry."""

    if record.kind is ScanRecordKind.ERROR:
        code = "unknown" if record.error_code is None else str(record.error_code)
        return (
            "FILESYSTEM_ERROR",
            f"{record.error or 'filesystem observation failed'} (code={code})",
            record.path,
        )
    if record.kind is ScanRecordKind.BOUNDARY:
        reason = record.boundary_reason or BoundaryReason.REPARSE_POINT
        return (
            f"BOUNDARY_{reason.value.upper()}",
            "Traversal stopped at a reparse or Cloud Files boundary; the target was not read.",
            record.path,
        )
    return None


__all__ = ["file_record_to_resource", "record_to_scan_error"]
