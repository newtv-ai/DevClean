from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import devclean.core.state as state_module
from devclean.core.models import (
    Confidence,
    EffectClass,
    Evidence,
    ProvenanceClass,
    Resource,
    RiskTier,
    ScanStatus,
    SemanticType,
    SizeValue,
    new_id,
)
from devclean.core.policy import build_inventory_plan
from devclean.core.state import StateStore
from devclean.evidence.models import CommandEvidence, EvidenceKind, TranscriptStorage
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.security import (
    audit_private_directory,
    audit_private_file,
)


def make_resource() -> Resource:
    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Fixture",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=r"C:\fixture",
        logical_size=SizeValue(12, Confidence.EXACT),
        allocated_size=SizeValue(4096, Confidence.EXACT),
    )


def test_state_store_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "state" / "DevClean.db"
    with StateStore(database) as store:
        scan_id = store.create_scan([r"C:\fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)
        store.add_scan_error(scan_id, "ACCESS_DENIED", "denied", r"C:\protected")
        plan = build_inventory_plan(scan_id, [resource])
        store.save_inventory_plan(plan)
        observed_at = datetime.now(UTC)
        run_id = store.add_adapter_run(
            scan_id=scan_id,
            adapter_id="pip",
            status="AVAILABLE",
            version="26.1.1",
            effect_class=EffectClass.PURE_QUERY,
            started_at=observed_at,
            finished_at=observed_at,
            payload={
                "completed": True,
                "executable": r"C:\Python\python.exe",
                "detail": "fixture",
                "resources": 1,
                "issues": [],
                "evidence_ids": [],
            },
        )
        store.finish_scan(scan_id, ScanStatus.COMPLETED, {"files": 1})

        scan = store.get_scan(scan_id)
        assert scan is not None
        assert scan["status"] == ScanStatus.COMPLETED.value
        assert scan["summary"] == {"files": 1}
        assert store.list_resources(scan_id)[0]["candidate_id"] == resource.candidate_id
        assert store.list_scan_errors(scan_id)[0]["kind"] == "ACCESS_DENIED"
        runs = list(store.iter_adapter_runs(scan_id))
        assert runs[0]["run_id"] == run_id
        assert store.count_adapter_runs(scan_id) == 1
        assert store.integrity_check()
        assert audit_private_directory(database.parent).policy_satisfied
        assert audit_private_file(database).policy_satisfied


def test_finished_scan_cannot_be_finished_twice(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        store.finish_scan(scan_id, ScanStatus.CANCELLED)
        with pytest.raises(KeyError):
            store.finish_scan(scan_id, ScanStatus.COMPLETED)


def test_adapter_run_rejects_invalid_status_and_time_order(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        observed_at = datetime.now(UTC)
        with pytest.raises(ValueError, match="status"):
            store.add_adapter_run(
                scan_id=scan_id,
                adapter_id="pip",
                status="RUNNING",
                version=None,
                effect_class=EffectClass.PURE_QUERY,
                started_at=observed_at,
                finished_at=observed_at,
                payload={},
            )


def test_state_store_rejects_out_of_schema_scan_and_adapter_fields(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError, match="scan root"):
            store.create_scan([""])
        with pytest.raises(ValueError, match="bounded sequence"):
            store.create_scan(["root"] * 1025)

        scan_id = store.create_scan(["fixture"])
        with pytest.raises(ValueError, match="scan error kind"):
            store.add_scan_error(scan_id, "bad/kind", "fixture", None)
        with pytest.raises(ValueError, match="unexpected or missing"):
            store.add_adapter_run(
                scan_id=scan_id,
                adapter_id="pip",
                status="AVAILABLE",
                version="26.1.1",
                effect_class=EffectClass.PURE_QUERY,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                payload={"completed": True},
            )


def test_tampered_inventory_plan_is_rejected_on_read(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        scan_id = store.create_scan(["fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)
        plan = build_inventory_plan(scan_id, [resource])
        store.save_inventory_plan(plan)

    connection = sqlite3.connect(database)
    try:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM plans WHERE plan_id = ?", (plan.plan_id,)
            ).fetchone()[0]
        )
        payload["executable"] = True
        connection.execute(
            "UPDATE plans SET payload_json = ? WHERE plan_id = ?",
            (json.dumps(payload), plan.plan_id),
        )
        connection.commit()
    finally:
        connection.close()

    with StateStore(database) as store, pytest.raises(
        RuntimeError, match="inventory-only"
    ):
        store.get_inventory_plan(plan.plan_id)


def test_tampered_inventory_plan_cannot_hide_maintenance_semantics(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        scan_id = store.create_scan(["fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)
        plan = build_inventory_plan(scan_id, [resource])
        store.save_inventory_plan(plan)
        payload = plan.to_dict()
        payload["actions"][0]["effect_class"] = "MAINTENANCE"
        store._connection.execute(
            "UPDATE plans SET payload_json = ? WHERE plan_id = ?",
            (json.dumps(payload), plan.plan_id),
        )
        store._connection.commit()

        with pytest.raises(RuntimeError, match="inventory-only"):
            store.get_inventory_plan(plan.plan_id)


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)
        with pytest.raises(sqlite3.IntegrityError):
            store.add_resource(scan_id, resource)
        assert len(store.list_resources(scan_id)) == 1


def test_state_store_rejects_actionable_resources(tmp_path: Path) -> None:
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="fixture",
        display_name="Future executable candidate",
        semantic_type=SemanticType.REBUILDABLE_CACHE,
        risk_tier=RiskTier.GREEN,
        provenance_class=ProvenanceClass.REGENERABLE_CONFIRMED,
        actionable=True,
    )
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        with pytest.raises(ValueError, match="rejects actionable"):
            store.add_resource(scan_id, resource)
        assert store.count_resources(scan_id) == 0


def test_batched_resources_and_errors_are_atomic(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        first = make_resource()
        second = make_resource()
        store.add_resources(scan_id, (first, second))
        store.add_scan_errors(
            scan_id,
            (
                ("BOUNDARY_REPARSE_POINT", "not traversed", r"C:\link"),
                ("FILESYSTEM_ERROR", "denied", r"C:\blocked"),
            ),
        )

        assert len(store.list_resources(scan_id)) == 2
        assert len(store.list_scan_errors(scan_id)) == 2

        with pytest.raises(sqlite3.IntegrityError):
            store.add_resources(scan_id, (first, make_resource()))
        assert len(store.list_resources(scan_id)) == 2


def test_state_store_rejects_unc_paths() -> None:
    with pytest.raises(ValueError, match="local filesystem"):
        StateStore(Path(r"\\server\share\DevClean.db"))


def test_state_store_rejects_non_fixed_or_redirected_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state_module, "is_local_fixed_path", lambda path: False)

    with pytest.raises(ValueError, match="fixed local volume"):
        StateStore(tmp_path / "state.db")


def test_version_one_database_is_backed_up_and_migrated(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    legacy = sqlite3.connect(database)
    legacy.executescript(state_module._MIGRATION_1)
    legacy.close()

    with StateStore(database) as store:
        assert store.schema_version() == state_module.DB_SCHEMA_VERSION
        assert store.integrity_check()

    backups = list(tmp_path.glob("state.db.pre-v1.bak*"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
    finally:
        backup.close()


def test_newer_database_schema_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "future.db"
    future = sqlite3.connect(database)
    future.executescript(state_module._MIGRATION_1)
    future.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", ("999",))
    future.commit()
    future.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        StateStore(database)


def test_command_evidence_round_trip_and_foreign_key(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        evidence = CommandEvidence(
            evidence_id="evidence_fixture",
            scan_id=scan_id,
            adapter_id="pip",
            kind=EvidenceKind.VENDOR_CLI,
            captured_at=datetime.now(UTC),
            executable_path=r"C:\Python\python.exe",
            executable_size=123,
            executable_mtime_ns=456,
            executable_volume_serial="1a2b",
            executable_file_id="3c4d",
            executable_file_id_kind="file_id_128",
            executable_sha256="c" * 64,
            argv_redacted=(r"C:\Python\python.exe", "-m", "pip", "--version"),
            effect_class=EffectClass.PURE_QUERY,
            returncode=0,
            duration_ms=7,
            timed_out=False,
            output_limit_exceeded=False,
            transcript_redaction_version="transcript-redaction-v1",
            stdout_size=10,
            stderr_size=0,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            stdout_stored_size=10,
            stderr_stored_size=0,
            stdout_stored_sha256="c" * 64,
            stderr_stored_sha256="d" * 64,
            stdout_storage=TranscriptStorage.REDACTED_UTF8,
            stderr_storage=TranscriptStorage.REDACTED_UTF8,
            stdout_file="commands/evidence_fixture.stdout.redacted.txt",
            stderr_file="commands/evidence_fixture.stderr.redacted.txt",
        )
        store.add_evidence(evidence)

        records = list(store.iter_evidence(scan_id, batch_size=1))
        assert records == [evidence.to_dict()]

        missing = replace(
            evidence,
            evidence_id="evidence_missing_scan",
            scan_id="scan_missing",
            stdout_file="commands/evidence_missing_scan.stdout.redacted.txt",
            stderr_file="commands/evidence_missing_scan.stderr.redacted.txt",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.add_evidence(missing)


def test_loopback_evidence_round_trip(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        evidence = EvidenceStore(scan_id, root=tmp_path / "evidence").record_loopback_response(
            adapter_id="ollama",
            endpoint="/api/version",
            status=200,
            duration_ms=1,
            body=b'{"version":"0.12.6"}',
            content_type="application/json",
            content_encoding=None,
        )

        store.add_evidence(evidence)

        assert list(store.iter_evidence(scan_id)) == [evidence.to_dict()]


def test_resource_and_adapter_evidence_references_must_exist_in_same_scan(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        other_scan_id = store.create_scan(["other"])
        record = EvidenceStore(
            other_scan_id, root=tmp_path / "evidence"
        ).record_loopback_response(
            adapter_id="ollama",
            endpoint="/api/version",
            status=200,
            duration_ms=1,
            body=b'{"version":"0.12.6"}',
            content_type="application/json",
            content_encoding=None,
        )
        store.add_evidence(record)
        resource = replace(
            make_resource(),
            evidence=(
                Evidence(
                    source=f"evidence:{record.evidence_id}",
                    detail="bounded fixture",
                    checked_at=datetime.now(UTC),
                ),
            ),
        )

        with pytest.raises(KeyError, match="not part of scan"):
            store.add_resource(scan_id, resource)

        observed_at = datetime.now(UTC)
        with pytest.raises(KeyError, match="not part of scan"):
            store.add_adapter_run(
                scan_id=scan_id,
                adapter_id="ollama",
                status="AVAILABLE",
                version="0.12.6",
                effect_class=EffectClass.PURE_QUERY,
                started_at=observed_at,
                finished_at=observed_at,
                payload={
                    "completed": True,
                    "executable": None,
                    "detail": "cross-scan fixture",
                    "resources": 0,
                    "issues": [],
                    "evidence_ids": [record.evidence_id],
                },
            )


def test_report_integrity_rejects_dangling_reference_after_database_tamper(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with StateStore(database) as store:
        scan_id = store.create_scan(["fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)

        payload = resource.to_dict()
        payload["evidence"] = [
            {
                "source": "evidence:evidence_missing",
                "detail": "tampered",
                "checked_at": datetime.now(UTC).isoformat(),
                "digest": None,
            }
        ]
        store._connection.execute(
            "UPDATE resources SET payload_json = ? WHERE candidate_id = ?",
            (json.dumps(payload), resource.candidate_id),
        )
        store._connection.commit()

        with pytest.raises(RuntimeError, match="invalid report integrity"):
            store.validate_evidence_references(scan_id)


def test_report_integrity_rejects_actionable_resource_database_tamper(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        resource = make_resource()
        store.add_resource(scan_id, resource)
        payload = resource.to_dict()
        payload["actionable"] = True
        store._connection.execute(
            """
            UPDATE resources
            SET actionable = 1, payload_json = ?
            WHERE candidate_id = ?
            """,
            (json.dumps(payload), resource.candidate_id),
        )
        store._connection.commit()

        with pytest.raises(RuntimeError, match="indexed inventory identity"):
            store.validate_evidence_references(scan_id)


def test_report_integrity_rejects_loopback_endpoint_database_tamper(
    tmp_path: Path,
) -> None:
    with StateStore(tmp_path / "state.db") as store:
        scan_id = store.create_scan(["fixture"])
        record = EvidenceStore(scan_id, root=tmp_path / "evidence").record_loopback_response(
            adapter_id="ollama",
            endpoint="/api/version",
            status=200,
            duration_ms=1,
            body=b'{"version":"0.12.6"}',
            content_type="application/json",
            content_encoding=None,
        )
        store.add_evidence(record)
        payload = record.to_dict()
        payload["endpoint"] = "/api/delete"
        store._connection.execute(
            "UPDATE evidence SET payload_json = ? WHERE evidence_id = ?",
            (json.dumps(payload), record.evidence_id),
        )
        store._connection.commit()

        with pytest.raises(RuntimeError, match="closed endpoint"):
            store.validate_evidence_references(scan_id)
