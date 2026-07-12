from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import reclaimer.core.reporting as reporting_module
from reclaimer.core.models import (
    Confidence,
    ProvenanceClass,
    Resource,
    RiskTier,
    ScanStatus,
    SemanticType,
    SizeValue,
    new_id,
)
from reclaimer.core.reporting import (
    build_report,
    iter_json_report,
    iter_markdown_report,
    render_json,
    render_markdown,
    write_report_stream,
)
from reclaimer.core.state import StateStore
from reclaimer.evidence.store import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]


def test_unsafe_legacy_report_writer_is_not_exposed() -> None:
    assert not hasattr(reporting_module, "write_report")


def test_report_is_redacted_and_non_executable(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\Users\Alice\Projects"])
        store.add_resource(
            scan_id,
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="filesystem",
                display_name="Large directory",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=r"C:\Users\Alice\Projects\private",
                logical_size=SizeValue(1024, Confidence.EXACT),
                allocated_size=SizeValue(4096, Confidence.EXACT),
            ),
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED)
        report = build_report(store, scan_id)

    assert report["safety_boundary"]["executable"] is False
    assert "Alice" not in render_json(report)
    markdown = render_markdown(report)
    assert "report-only" in markdown
    assert "4.0 KiB" in markdown


def test_unknown_scan_is_rejected(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        try:
            build_report(store, "scan_missing")
        except KeyError as error:
            assert "scan_missing" in str(error)
        else:
            raise AssertionError("missing scan should be rejected")


def test_markdown_escapes_terminal_html_and_bidi_controls(tmp_path: Path) -> None:
    path = "payload\x1b]8;;https://evil.example\x07|<img>\u202ereversed"
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\fixture"])
        store.add_resource(
            scan_id,
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="filesystem",
                display_name="Filesystem file",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=path,
            ),
        )
        store.add_scan_error(
            scan_id,
            "OBSERVATION",
            "line1\nline2\x1b[31m<script>\u2066",
            path,
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED)
        markdown = render_markdown(build_report(store, scan_id, redact=False))

    assert "\x1b" not in markdown
    assert "\x07" not in markdown
    assert "\u202e" not in markdown
    assert "\u2066" not in markdown
    assert r"\u001b" in markdown
    assert r"\u0007" in markdown
    assert r"\u202e" in markdown
    assert r"\u2066" in markdown
    assert r"\|" in markdown
    assert "&lt;img&gt;" in markdown
    assert "&lt;script&gt;" in markdown


def test_full_paths_never_disable_secret_redaction(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\Users\Alice\project"])
        store.add_resource(
            scan_id,
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="filesystem",
                display_name="Filesystem file",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=r"C:\Users\Alice\project\token=supersecret",
            ),
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

        report = build_report(store, scan_id, redact=False)
        rendered = render_json(report)

    assert "Alice" in rendered
    assert "supersecret" not in rendered
    assert "<REDACTED>" in rendered


def test_streaming_reports_are_valid_and_complete(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\Users\Alice\project"])
        resources = [
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="filesystem",
                display_name="Filesystem file",
                semantic_type=SemanticType.UNKNOWN,
                risk_tier=RiskTier.RED,
                provenance_class=ProvenanceClass.UNKNOWN,
                path=rf"C:\Users\Alice\project\file-{index}.bin",
                logical_size=SizeValue(index, Confidence.EXACT),
            )
            for index in range(17)
        ]
        store.add_resources(scan_id, resources)
        store.add_scan_error(scan_id, "BOUNDARY_REPARSE_POINT", "not read", r"C:\link")
        store.finish_scan(scan_id, ScanStatus.COMPLETED, {"files": len(resources)})

        streamed_json = "".join(iter_json_report(store, scan_id))
        streamed_markdown = "".join(iter_markdown_report(store, scan_id))

    payload = json.loads(streamed_json)
    assert len(payload["resources"]) == 17
    assert len(payload["errors"]) == 1
    assert payload["safety_boundary"]["executable"] is False
    assert "Alice" not in streamed_json
    assert "Resources: 17" in streamed_markdown
    assert "Errors/boundaries: 1" in streamed_markdown


def test_redacted_report_preserves_valid_loopback_evidence_identity(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\fixture"])
        evidence = EvidenceStore(
            scan_id, root=tmp_path / "evidence"
        ).record_loopback_response(
            adapter_id="ollama",
            endpoint="/api/version",
            status=200,
            duration_ms=1,
            body=b'{"version":"0.12.6"}',
            content_type="application/json",
            content_encoding=None,
        )
        store.add_evidence(evidence)
        store.finish_scan(scan_id, ScanStatus.COMPLETED)

        report = build_report(store, scan_id)

    assert report["scan"]["scan_id"] == scan_id
    record = report["evidence"][0]
    assert record["evidence_id"] == evidence.evidence_id
    assert record["response_sha256"] == evidence.response_sha256
    assert record["response_stored_sha256"] == evidence.response_stored_sha256
    assert record["response_file"] == evidence.response_file

    report_schema = json.loads(
        (ROOT / "schemas" / "scan-report.schema.json").read_text(encoding="utf-8")
    )
    evidence_schema = {
        "$schema": report_schema["$schema"],
        "$defs": report_schema["$defs"],
        "$ref": "#/$defs/evidence_record",
    }
    Draft202012Validator(evidence_schema).validate(record)
    markdown = render_markdown(report)
    assert "`GET /api/version` RESPONSE HTTP 200" in markdown


def test_streaming_report_fails_before_emitting_dangling_evidence_reference(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan([r"C:\fixture"])
        resource = Resource(
            candidate_id=new_id("candidate"),
            adapter_id="filesystem",
            display_name="Fixture",
            semantic_type=SemanticType.UNKNOWN,
            risk_tier=RiskTier.RED,
            provenance_class=ProvenanceClass.UNKNOWN,
        )
        store.add_resource(scan_id, resource)
        payload = resource.to_dict()
        payload["evidence"] = [
            {
                "source": "evidence:evidence_missing",
                "detail": "tampered",
                "checked_at": "2026-07-11T00:00:00+00:00",
                "digest": None,
            }
        ]
        store._connection.execute(
            "UPDATE resources SET payload_json = ? WHERE candidate_id = ?",
            (json.dumps(payload), resource.candidate_id),
        )
        store._connection.commit()

        stream = iter_json_report(store, scan_id)
        try:
            next(stream)
        except RuntimeError as error:
            assert "invalid report integrity" in str(error)
        else:
            raise AssertionError("stream emitted bytes before integrity validation")


def test_report_stream_is_atomic_and_never_overwrites(tmp_path: Path) -> None:
    destination = tmp_path / "reports" / "inventory.json"
    write_report_stream(destination, ("{\n", '  "safe": true\n', "}\n"))

    assert destination.read_text(encoding="utf-8") == '{\n  "safe": true\n}\n'
    assert not tuple(destination.parent.glob(".*.tmp"))
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_report_stream(destination, ("replacement",))
    assert destination.read_text(encoding="utf-8") == '{\n  "safe": true\n}\n'


def test_report_stream_cleans_private_temp_on_render_failure(tmp_path: Path) -> None:
    destination = tmp_path / "report.json"

    def failing_chunks():
        yield "partial"
        raise RuntimeError("fixture render failure")

    with pytest.raises(RuntimeError, match="fixture render failure"):
        write_report_stream(destination, failing_chunks())

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".*.tmp"))


def test_report_stream_rejects_nonlocal_or_reparse_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reporting_module, "is_local_fixed_path", lambda _path: False)

    with pytest.raises(ValueError, match="fixed local path"):
        write_report_stream(tmp_path / "report.json", ("safe",))
    assert not (tmp_path / "report.json").exists()
