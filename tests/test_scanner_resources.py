from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from reclaimer.scanner import BoundaryReason, ScanRecord, ScanRecordKind
from reclaimer.scanner.resources import file_record_to_resource, record_to_scan_error


def test_file_record_maps_to_red_unknown_non_actionable_resource() -> None:
    record = ScanRecord(
        root=r"C:\fixture",
        path=r"C:\fixture\payload.bin",
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=7,
        allocated_size=4096,
        raw_allocated_size=4096,
        volume_serial=42,
        file_id="abcdef",
        file_id_kind="file_id_128",
        link_count=1,
        attributes=32,
        creation_time_ns=100,
        last_write_time_ns=200,
    )

    resource = file_record_to_resource(record)

    assert resource.semantic_type.value == "UNKNOWN"
    assert resource.risk_tier.value == "RED"
    assert resource.actionable is False
    assert resource.identity is not None
    assert resource.identity.file_id_kind == "file_id_128"
    assert resource.identity.creation_time_ns == 100
    assert resource.identity.last_write_time_ns == 200


def test_hardlink_raw_allocation_and_aggregate_uncertainty_are_kept_separate() -> None:
    record = ScanRecord(
        root=r"C:\fixture",
        path=r"C:\fixture\payload.bin",
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=7,
        allocated_size=None,
        raw_allocated_size=4096,
        allocation_uncertain=True,
    )

    resource = file_record_to_resource(record)

    assert resource.allocated_size.value == 4096
    assert resource.allocated_size.confidence.value == "EXACT"
    assert any("identity accounting" in warning for warning in resource.warnings)


def test_boundary_becomes_durable_non_traversal_entry() -> None:
    record = ScanRecord(
        root=r"C:\fixture",
        path=r"C:\fixture\link",
        kind=ScanRecordKind.BOUNDARY,
        depth=1,
        boundary_reason=BoundaryReason.REPARSE_POINT,
    )

    error = record_to_scan_error(record)

    assert error is not None
    assert error[0] == "BOUNDARY_REPARSE_POINT"
    assert "not read" in error[1]


def test_mapped_resource_validates_against_schema() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "resource.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    record = ScanRecord(
        root=r"C:\fixture",
        path=r"C:\fixture\payload.bin",
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=1,
        allocated_size=4096,
        raw_allocated_size=4096,
    )

    Draft202012Validator(schema).validate(file_record_to_resource(record).to_dict())
