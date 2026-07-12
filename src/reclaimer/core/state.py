"""Single-writer SQLite state store for scans and future durable intent records."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reclaimer import __version__
from reclaimer.core.models import EffectClass, Plan, Resource, ScanStatus, new_id, utc_now
from reclaimer.core.paths import state_path
from reclaimer.evidence.models import CommandEvidence, EvidenceRecord, LoopbackEvidence
from reclaimer.platform.windows.security import (
    secure_private_directory,
    secure_private_file,
)
from reclaimer.platform.windows.volumes import is_local_fixed_path

DB_SCHEMA_VERSION = 2
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ADAPTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ERROR_KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ISSUE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_]{0,127}$")
_FAILURE_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
_EVIDENCE_REFERENCE = re.compile(
    r"^evidence:(evidence_[A-Za-z0-9][A-Za-z0-9._:-]{0,246})$"
)
_MAX_PLAN_SELECTION = 256
_REFERENCE_QUERY_BATCH = 400
_MAX_SCAN_ROOTS = 1024
_MAX_RESOURCE_BATCH = 10_000
_MAX_SCAN_ERROR_BATCH = 10_000
_MAX_ADAPTER_ISSUES = 100_000
_MAX_ADAPTER_EVIDENCE_IDS = 100_000
_MAX_SUMMARY_BYTES = 4 * 1024 * 1024


def _json_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _json_load_object(raw: object, field_name: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise RuntimeError(f"stored {field_name} is not JSON text")
    try:
        payload = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"stored {field_name} is not strict JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"stored {field_name} is not a JSON object")
    return payload


def _parse_aware_datetime_text(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError(f"stored {field_name} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError(f"stored {field_name} is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"stored {field_name} is not timezone-aware")
    return parsed


def _require_text(
    value: object,
    field_name: str,
    *,
    min_length: int = 0,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not min_length <= len(value) <= max_length:
        raise ValueError(f"{field_name} is not bounded text")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _require_optional_text(
    value: object, field_name: str, *, max_length: int
) -> str | None:
    if value is None:
        return None
    return _require_text(value, field_name, max_length=max_length)


def _require_non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _validate_adapter_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("adapter payload must be an object")
    completed = payload.get("completed")
    if not isinstance(completed, bool):
        raise ValueError("adapter payload completed must be a boolean")
    if completed:
        expected_keys = {
            "completed",
            "executable",
            "detail",
            "resources",
            "issues",
            "evidence_ids",
        }
        if set(payload) != expected_keys:
            raise ValueError("completed adapter payload has unexpected or missing fields")
        _require_optional_text(
            payload["executable"], "adapter executable", max_length=32_767
        )
        _require_optional_text(payload["detail"], "adapter detail", max_length=8192)
        _require_non_negative_integer(payload["resources"], "adapter resources")
        issues = payload["issues"]
        if not isinstance(issues, list) or len(issues) > _MAX_ADAPTER_ISSUES:
            raise ValueError("adapter issues must be a bounded list")
        for issue in issues:
            if not isinstance(issue, dict) or set(issue) != {"code", "message", "fatal"}:
                raise ValueError("adapter issue does not match the closed contract")
            _require_text(
                issue["code"],
                "adapter issue code",
                min_length=1,
                max_length=128,
                pattern=_ISSUE_CODE,
            )
            _require_text(
                issue["message"],
                "adapter issue message",
                min_length=1,
                max_length=8192,
            )
            if not isinstance(issue["fatal"], bool):
                raise ValueError("adapter issue fatal must be a boolean")
        evidence_ids = payload["evidence_ids"]
        if (
            not isinstance(evidence_ids, list)
            or len(evidence_ids) > _MAX_ADAPTER_EVIDENCE_IDS
        ):
            raise ValueError("adapter evidence_ids must be a bounded list")
    else:
        if set(payload) != {"completed", "failure_type"}:
            raise ValueError("failed adapter payload has unexpected or missing fields")
        _require_text(
            payload["failure_type"],
            "adapter failure_type",
            min_length=1,
            max_length=128,
            pattern=_FAILURE_TYPE,
        )
    return payload

_MIGRATION_1 = """
BEGIN IMMEDIATE;

CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE scans (
    scan_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    engine_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    roots_json TEXT NOT NULL,
    summary_json TEXT
);

CREATE TABLE resources (
    candidate_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    adapter_id TEXT NOT NULL,
    semantic_type TEXT NOT NULL,
    risk_tier TEXT NOT NULL,
    path TEXT,
    logical_size INTEGER,
    allocated_size INTEGER,
    actionable INTEGER NOT NULL CHECK (actionable IN (0, 1)),
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_resources_scan ON resources(scan_id);
CREATE INDEX idx_resources_adapter ON resources(adapter_id);

CREATE TABLE scan_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    path TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE plans (
    plan_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

INSERT INTO schema_meta(key, value) VALUES('schema_version', '1');
COMMIT;
"""

_MIGRATION_2 = """
BEGIN IMMEDIATE;

CREATE TABLE adapter_runs (
    run_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    adapter_id TEXT NOT NULL,
    status TEXT NOT NULL,
    version TEXT,
    effect_class TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_adapter_runs_scan ON adapter_runs(scan_id);
CREATE INDEX idx_adapter_runs_adapter ON adapter_runs(adapter_id);

CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(scan_id) ON DELETE CASCADE,
    adapter_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX idx_evidence_scan ON evidence(scan_id);
CREATE INDEX idx_evidence_adapter ON evidence(adapter_id);

UPDATE schema_meta SET value = '2' WHERE key = 'schema_version';
COMMIT;
"""

_REQUIRED_TABLES = frozenset(
    {
        "adapter_runs",
        "evidence",
        "plans",
        "resources",
        "scan_errors",
        "scans",
        "schema_meta",
    }
)


class StateStore:
    """Own a local SQLite connection configured for fail-closed, single-writer use."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_path()
        if not self.path.is_absolute() or str(self.path).startswith((r"\\", "//")):
            raise ValueError("state database must use an absolute path on a local filesystem")
        if not is_local_fixed_path(self.path.parent):
            raise ValueError(
                "state database must use a fixed local volume without reparse ancestors"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        secure_private_directory(self.path.parent)
        if self.path.exists():
            secure_private_file(self.path)
        self._connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            # A fresh database inherits the private directory DACL. Protecting the
            # file itself also repairs a pre-existing database whose old inherited
            # ACL no longer matches the now-protected parent.
            secure_private_file(self.path)
            self._connection.row_factory = sqlite3.Row
            self._configure()
            self._migrate()
        except BaseException:
            self._connection.close()
            raise

    def _configure(self) -> None:
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def _migrate(self) -> None:
        tables = self._table_names()
        fresh = not tables
        if fresh:
            self._apply_migration(_MIGRATION_1)
        elif "schema_meta" not in tables:
            raise RuntimeError("state database has tables but no Reclaimer schema metadata")

        version = self._read_schema_version()
        if version > DB_SCHEMA_VERSION:
            raise RuntimeError(
                f"state schema {version} is newer than supported {DB_SCHEMA_VERSION}"
            )
        migrations = {2: _MIGRATION_2}
        while version < DB_SCHEMA_VERSION:
            target = version + 1
            migration = migrations.get(target)
            if migration is None:
                raise RuntimeError(f"no state migration from schema {version} to {target}")
            if not fresh:
                self._backup_before_migration(version)
            self._apply_migration(migration)
            version = self._read_schema_version()
            if version != target:
                raise RuntimeError(
                    f"state migration reported schema {version}; expected {target}"
                )

        missing = _REQUIRED_TABLES - self._table_names()
        if missing:
            raise RuntimeError(f"state schema is missing required tables: {sorted(missing)}")

    def _apply_migration(self, script: str) -> None:
        try:
            self._connection.executescript(script)
        except BaseException:
            self._connection.rollback()
            raise

    def _read_schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("state database is missing its schema version")
        try:
            return int(row["value"])
        except (TypeError, ValueError) as error:
            raise RuntimeError("state database has an invalid schema version") from error

    def _table_names(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return {str(row["name"]) for row in rows}

    def _backup_before_migration(self, version: int) -> Path:
        stem = f"{self.path.name}.pre-v{version}.bak"
        target = self.path.with_name(stem)
        suffix = 1
        while target.exists():
            target = self.path.with_name(f"{stem}.{suffix}")
            suffix += 1
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        destination = sqlite3.connect(temporary)
        try:
            self._connection.backup(destination)
            destination.close()
            os.replace(temporary, target)
            secure_private_file(target)
        except BaseException:
            destination.close()
            temporary.unlink(missing_ok=True)
            raise
        return target

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def create_scan(self, roots: Sequence[str]) -> str:
        if isinstance(roots, (str, bytes)) or len(roots) > _MAX_SCAN_ROOTS:
            raise ValueError("scan roots must be a bounded sequence")
        normalized_roots = [
            _require_text(root, "scan root", min_length=1, max_length=32_767)
            for root in roots
        ]
        scan_id = new_id("scan")
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO scans(
                    scan_id, schema_version, engine_version, status, started_at, roots_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    "1.0.0",
                    __version__,
                    ScanStatus.RUNNING.value,
                    utc_now().isoformat(),
                    _json_dumps(normalized_roots),
                ),
            )
        return scan_id

    def add_resource(self, scan_id: str, resource: Resource) -> None:
        self.add_resources(scan_id, (resource,))

    def add_resources(self, scan_id: str, resources: Sequence[Resource]) -> None:
        """Persist a bounded batch of resources in one durable transaction.

        Callers retain control of the batch size so a large scan can stream to SQLite
        without either one transaction per file or an unbounded in-memory collection.
        """

        if not resources:
            return
        if len(resources) > _MAX_RESOURCE_BATCH:
            raise ValueError("resource batch exceeds its bound")
        if any(resource.actionable for resource in resources):
            raise ValueError("the current state store rejects actionable resources")
        rows = [self._resource_row(scan_id, resource) for resource in resources]
        evidence_ids = [
            evidence_id
            for resource in resources
            for evidence_id in self._resource_evidence_ids(resource.to_dict())
        ]
        with self.transaction():
            self._require_evidence_ids(scan_id, evidence_ids)
            self._connection.executemany(
                """
                INSERT INTO resources(
                    candidate_id, scan_id, adapter_id, semantic_type, risk_tier, path,
                    logical_size, allocated_size, actionable, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    @staticmethod
    def _resource_row(scan_id: str, resource: Resource) -> tuple[object, ...]:
        payload = resource.to_dict()
        return (
            resource.candidate_id,
            scan_id,
            resource.adapter_id,
            resource.semantic_type.value,
            resource.risk_tier.value,
            resource.path,
            resource.logical_size.value,
            resource.allocated_size.value,
            int(resource.actionable),
            _json_dumps(payload),
        )

    def add_scan_error(self, scan_id: str, kind: str, message: str, path: str | None) -> None:
        self.add_scan_errors(scan_id, ((kind, message, path),))

    def add_scan_errors(
        self,
        scan_id: str,
        errors: Sequence[tuple[str, str, str | None]],
    ) -> None:
        """Persist a bounded batch of observational errors or traversal boundaries."""

        if not errors:
            return
        if len(errors) > _MAX_SCAN_ERROR_BATCH:
            raise ValueError("scan error batch exceeds its bound")
        normalized_errors: list[tuple[str, str, str | None]] = []
        for error in errors:
            if not isinstance(error, tuple) or len(error) != 3:
                raise ValueError("scan errors must be three-item tuples")
            kind, message, path = error
            normalized_errors.append(
                (
                    _require_text(
                        kind,
                        "scan error kind",
                        min_length=1,
                        max_length=128,
                        pattern=_ERROR_KIND,
                    ),
                    _require_text(
                        message,
                        "scan error message",
                        min_length=1,
                        max_length=8192,
                    ),
                    _require_optional_text(path, "scan error path", max_length=32_767),
                )
            )
        with self.transaction():
            self._connection.executemany(
                "INSERT INTO scan_errors(scan_id, path, kind, message) VALUES(?, ?, ?, ?)",
                (
                    (scan_id, path, kind, message)
                    for kind, message, path in normalized_errors
                ),
            )

    def finish_scan(
        self, scan_id: str, status: ScanStatus, summary: dict[str, Any] | None = None
    ) -> None:
        if not isinstance(status, ScanStatus):
            raise ValueError("finish_scan status must be a ScanStatus")
        if status is ScanStatus.RUNNING:
            raise ValueError("finish_scan requires a terminal status")
        if summary is not None and not isinstance(summary, dict):
            raise ValueError("scan summary must be an object")
        summary_json = _json_dumps(summary or {})
        if len(summary_json.encode("utf-8")) > _MAX_SUMMARY_BYTES:
            raise ValueError("scan summary exceeds its byte bound")
        with self.transaction():
            cursor = self._connection.execute(
                """
                UPDATE scans
                SET status = ?, finished_at = ?, summary_json = ?
                WHERE scan_id = ? AND status = ?
                """,
                (
                    status.value,
                    utc_now().isoformat(),
                    summary_json,
                    scan_id,
                    ScanStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"scan is missing or already finished: {scan_id}")

    def save_inventory_plan(self, plan: Plan) -> None:
        if not plan.is_inventory_only:
            raise ValueError("the current milestone stores inventory-only plans")
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO plans(plan_id, scan_id, created_at, expires_at, payload_json)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.scan_id,
                    plan.created_at.isoformat(),
                    plan.expires_at.isoformat(),
                    _json_dumps(plan.to_dict()),
                ),
            )

    def add_adapter_run(
        self,
        *,
        scan_id: str,
        adapter_id: str,
        status: str,
        version: str | None,
        effect_class: EffectClass,
        started_at: datetime,
        finished_at: datetime,
        payload: dict[str, Any],
    ) -> str:
        """Persist one completed inventory-adapter observation."""

        allowed_statuses = {"AVAILABLE", "UNAVAILABLE", "UNSUPPORTED_VERSION", "ERROR"}
        _require_text(
            scan_id,
            "adapter scan_id",
            min_length=1,
            max_length=256,
            pattern=_SAFE_ID,
        )
        _require_text(
            adapter_id,
            "adapter_id",
            min_length=1,
            max_length=64,
            pattern=_ADAPTER_ID,
        )
        if status not in allowed_statuses:
            raise ValueError("adapter run status is not recognized")
        _require_optional_text(version, "adapter version", max_length=128)
        if effect_class not in {
            EffectClass.PURE_QUERY,
            EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        }:
            raise ValueError("adapter inventory cannot use a maintenance effect class")
        if (
            not isinstance(started_at, datetime)
            or started_at.tzinfo is None
            or started_at.utcoffset() is None
            or not isinstance(finished_at, datetime)
            or finished_at.tzinfo is None
            or finished_at.utcoffset() is None
        ):
            raise ValueError("adapter run timestamps must be timezone-aware")
        if finished_at < started_at:
            raise ValueError("adapter run cannot finish before it starts")
        normalized_input = _validate_adapter_payload(payload)
        evidence_ids = self._adapter_run_evidence_ids(normalized_input)
        self._require_evidence_ids(scan_id, evidence_ids)

        run_id = new_id("adapter_run")
        normalized_payload = {
            **normalized_input,
            "run_id": run_id,
            "scan_id": scan_id,
            "adapter_id": adapter_id,
            "status": status,
            "version": version,
            "effect_class": effect_class.value,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
        }
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO adapter_runs(
                    run_id, scan_id, adapter_id, status, version, effect_class,
                    started_at, finished_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scan_id,
                    adapter_id,
                    status,
                    version,
                    effect_class.value,
                    started_at.isoformat(),
                    finished_at.isoformat(),
                    _json_dumps(normalized_payload),
                ),
            )
        return run_id

    def add_evidence(self, evidence: EvidenceRecord) -> None:
        if not isinstance(evidence, (CommandEvidence, LoopbackEvidence)):
            raise ValueError("state evidence must use a closed evidence model")
        payload = evidence.to_dict()
        with self.transaction():
            self._connection.execute(
                """
                INSERT INTO evidence(
                    evidence_id, scan_id, adapter_id, kind, captured_at, payload_json
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.scan_id,
                    evidence.adapter_id,
                    evidence.kind.value,
                    evidence.captured_at.isoformat(),
                    _json_dumps(payload),
                ),
            )

    def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM scans WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            roots = json.loads(
                result.pop("roots_json"), parse_constant=_reject_json_constant
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError("stored scan roots are not strict JSON") from error
        if (
            not isinstance(roots, list)
            or len(roots) > _MAX_SCAN_ROOTS
            or any(
                not isinstance(root, str) or not 1 <= len(root) <= 32_767
                for root in roots
            )
        ):
            raise RuntimeError("stored scan roots violate the report contract")
        summary = _json_load_object(result.pop("summary_json") or "{}", "scan summary")
        if (
            result.get("scan_id") != scan_id
            or not _SAFE_ID.fullmatch(scan_id)
            or result.get("schema_version") != "1.0.0"
            or not isinstance(result.get("engine_version"), str)
            or not 1 <= len(result["engine_version"]) <= 128
            or result.get("status") not in {status.value for status in ScanStatus}
        ):
            raise RuntimeError("stored scan identity violates the report contract")
        _parse_aware_datetime_text(result.get("started_at"), "scan started_at")
        finished_at = result.get("finished_at")
        if finished_at is not None:
            _parse_aware_datetime_text(finished_at, "scan finished_at")
        if (result["status"] == ScanStatus.RUNNING.value) != (finished_at is None):
            raise RuntimeError("stored scan status and completion timestamp disagree")
        result["roots"] = roots
        result["summary"] = summary
        return result

    def list_resources(self, scan_id: str) -> list[dict[str, Any]]:
        return list(self.iter_resources(scan_id))

    def get_resources_by_ids(
        self, scan_id: str, candidate_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Fetch a bounded exact selection without accepting paths or query fragments."""

        if not candidate_ids or len(candidate_ids) > _MAX_PLAN_SELECTION:
            raise ValueError("plan selection must contain between 1 and 256 candidates")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("plan selection contains duplicate candidate IDs")
        if any(not _SAFE_ID.fullmatch(candidate_id) for candidate_id in candidate_ids):
            raise ValueError("plan selection contains an invalid candidate ID")
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self._connection.execute(
            f"""
            SELECT candidate_id, adapter_id, semantic_type, risk_tier, path,
                   logical_size, allocated_size, actionable, payload_json
            FROM resources
            WHERE scan_id = ? AND candidate_id IN ({placeholders})
            """,
            (scan_id, *candidate_ids),
        ).fetchall()
        by_id = {
            str(row["candidate_id"]): self._validated_resource_payload(row)
            for row in rows
        }
        missing = [candidate_id for candidate_id in candidate_ids if candidate_id not in by_id]
        if missing:
            raise KeyError(f"selected candidates are not part of scan {scan_id}: {missing}")
        return [by_id[candidate_id] for candidate_id in candidate_ids]

    def iter_resources(
        self, scan_id: str, *, batch_size: int = 512
    ) -> Iterator[dict[str, Any]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cursor = self._connection.execute(
            """
            SELECT candidate_id, adapter_id, semantic_type, risk_tier, path,
                   logical_size, allocated_size, actionable, payload_json
            FROM resources
            WHERE scan_id = ?
            ORDER BY candidate_id
            """,
            (scan_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                yield self._validated_resource_payload(row)

    @staticmethod
    def _validated_resource_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = _json_load_object(row["payload_json"], "resource payload")
        logical_size = payload.get("logical_size")
        allocated_size = payload.get("allocated_size")
        if (
            payload.get("candidate_id") != row["candidate_id"]
            or payload.get("adapter_id") != row["adapter_id"]
            or payload.get("semantic_type") != row["semantic_type"]
            or payload.get("risk_tier") != row["risk_tier"]
            or payload.get("path") != row["path"]
            or not isinstance(logical_size, dict)
            or logical_size.get("value") != row["logical_size"]
            or not isinstance(allocated_size, dict)
            or allocated_size.get("value") != row["allocated_size"]
            or row["actionable"] != 0
            or payload.get("actionable") is not False
        ):
            raise RuntimeError("stored resource violates indexed inventory identity")
        return payload

    def list_scan_errors(self, scan_id: str) -> list[dict[str, Any]]:
        return list(self.iter_scan_errors(scan_id))

    def iter_scan_errors(
        self, scan_id: str, *, batch_size: int = 512
    ) -> Iterator[dict[str, Any]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cursor = self._connection.execute(
            "SELECT path, kind, message FROM scan_errors WHERE scan_id = ? ORDER BY error_id",
            (scan_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                payload = dict(row)
                try:
                    _require_optional_text(
                        payload.get("path"), "scan error path", max_length=32_767
                    )
                    _require_text(
                        payload.get("kind"),
                        "scan error kind",
                        min_length=1,
                        max_length=128,
                        pattern=_ERROR_KIND,
                    )
                    _require_text(
                        payload.get("message"),
                        "scan error message",
                        min_length=1,
                        max_length=8192,
                    )
                except ValueError as error:
                    raise RuntimeError(
                        "stored scan error violates the report contract"
                    ) from error
                yield payload

    def count_resources(self, scan_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM resources WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def count_scan_errors(self, scan_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM scan_errors WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def iter_adapter_runs(
        self, scan_id: str, *, batch_size: int = 128
    ) -> Iterator[dict[str, Any]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cursor = self._connection.execute(
            """
            SELECT run_id, scan_id, adapter_id, status, version, effect_class,
                   started_at, finished_at, payload_json
            FROM adapter_runs
            WHERE scan_id = ?
            ORDER BY started_at, run_id
            """,
            (scan_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                payload = _json_load_object(row["payload_json"], "adapter-run payload")
                if any(
                    payload.get(field_name) != row[field_name]
                    for field_name in (
                        "run_id",
                        "scan_id",
                        "adapter_id",
                        "status",
                        "version",
                        "effect_class",
                        "started_at",
                        "finished_at",
                    )
                ) or payload.get("effect_class") not in {
                    EffectClass.PURE_QUERY.value,
                    EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES.value,
                }:
                    raise RuntimeError(
                        "stored adapter run violates indexed inventory identity"
                    )
                base_fields = {
                    "run_id",
                    "scan_id",
                    "adapter_id",
                    "status",
                    "version",
                    "effect_class",
                    "started_at",
                    "finished_at",
                }
                try:
                    _validate_adapter_payload(
                        {key: value for key, value in payload.items() if key not in base_fields}
                    )
                except ValueError as error:
                    raise RuntimeError(
                        "stored adapter run violates the closed payload contract"
                    ) from error
                yield payload

    def count_adapter_runs(self, scan_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM adapter_runs WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def get_inventory_plan(self, plan_id: str) -> dict[str, Any] | None:
        if not _SAFE_ID.fullmatch(plan_id):
            raise ValueError("plan_id is invalid")
        row = self._connection.execute(
            "SELECT payload_json FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict) or payload.get("plan_id") != plan_id:
            raise RuntimeError("stored inventory plan identity is invalid")
        actions = payload.get("actions")
        if (
            payload.get("executable") is not False
            or not isinstance(actions, list)
            or not actions
            or any(
                not isinstance(action, dict)
                or action.get("kind") != "REPORT_ONLY"
                or action.get("enabled") is not False
                or action.get("effect_class") != "PURE_QUERY"
                or action.get("selection_mode") != "NONE"
                or action.get("preview_mode") != "NONE"
                or action.get("reclaim_scope") != "UNKNOWN"
                for action in actions
            )
        ):
            raise RuntimeError("stored plan violates the inventory-only safety boundary")
        return payload

    def iter_evidence(
        self, scan_id: str, *, batch_size: int = 128
    ) -> Iterator[dict[str, Any]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cursor = self._connection.execute(
            """
            SELECT evidence_id, scan_id, adapter_id, kind, captured_at, payload_json
            FROM evidence
            WHERE scan_id = ?
            ORDER BY captured_at, evidence_id
            """,
            (scan_id,),
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                payload = _json_load_object(row["payload_json"], "evidence payload")
                if any(
                    payload.get(field_name) != row[field_name]
                    for field_name in (
                        "evidence_id",
                        "scan_id",
                        "adapter_id",
                        "kind",
                        "captured_at",
                    )
                ):
                    raise RuntimeError("stored evidence violates indexed identity")
                kind = payload.get("kind")
                effect_class = payload.get("effect_class")
                if kind == "VENDOR_CLI":
                    if effect_class not in {
                        EffectClass.PURE_QUERY.value,
                        EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES.value,
                    }:
                        raise RuntimeError("stored command evidence is not observational")
                elif kind == "LOOPBACK_API":
                    if (
                        effect_class != EffectClass.PURE_QUERY.value
                        or payload.get("host") != "127.0.0.1"
                        or payload.get("port") != 11434
                        or payload.get("method") != "GET"
                        or payload.get("endpoint")
                        not in {"/api/version", "/api/tags", "/api/ps"}
                    ):
                        raise RuntimeError("stored loopback evidence violates its closed endpoint")
                else:
                    raise RuntimeError("stored evidence kind is not supported")
                yield payload

    def count_evidence(self, scan_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM evidence WHERE scan_id = ?", (scan_id,)
        ).fetchone()
        return 0 if row is None else int(row["count"])

    def validate_evidence_references(self, scan_id: str) -> None:
        """Fail when report rows or their evidence links violate closed invariants.

        JSON Schema cannot express a cross-array foreign key.  This bounded streaming
        pass supplies that invariant and rechecks indexed identity/safety fields without
        materializing the resource set. It is intentionally run before a report emits
        its first byte.
        """

        if self.get_scan(scan_id) is None:
            raise KeyError(f"unknown scan: {scan_id}")
        pending: list[str] = []
        try:
            for resource in self.iter_resources(scan_id):
                pending.extend(self._resource_evidence_ids(resource))
                if len(pending) >= _REFERENCE_QUERY_BATCH:
                    self._require_evidence_ids(scan_id, pending)
                    pending.clear()
            for adapter_run in self.iter_adapter_runs(scan_id):
                pending.extend(self._adapter_run_evidence_ids(adapter_run))
                if len(pending) >= _REFERENCE_QUERY_BATCH:
                    self._require_evidence_ids(scan_id, pending)
                    pending.clear()
            self._require_evidence_ids(scan_id, pending)
            for _error in self.iter_scan_errors(scan_id):
                pass
            for _evidence in self.iter_evidence(scan_id):
                pass
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"scan {scan_id} contains invalid report integrity or evidence references"
            ) from error

    @staticmethod
    def _resource_evidence_ids(payload: dict[str, Any]) -> tuple[str, ...]:
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise TypeError("resource evidence must be a list")
        references: list[str] = []
        for item in evidence:
            if not isinstance(item, dict):
                raise TypeError("resource evidence entries must be objects")
            source = item.get("source")
            if not isinstance(source, str):
                raise TypeError("resource evidence source must be text")
            if source.startswith("evidence:"):
                match = _EVIDENCE_REFERENCE.fullmatch(source)
                if match is None:
                    raise ValueError("resource evidence reference is malformed")
                references.append(match.group(1))
        return tuple(references)

    @staticmethod
    def _adapter_run_evidence_ids(payload: dict[str, Any]) -> tuple[str, ...]:
        value = payload.get("evidence_ids")
        if value is None:
            return ()
        if not isinstance(value, list):
            raise TypeError("adapter evidence_ids must be a list")
        if len(value) > _MAX_ADAPTER_EVIDENCE_IDS:
            raise ValueError("adapter evidence_ids exceeds its bound")
        if any(
            not isinstance(evidence_id, str)
            or not evidence_id.startswith("evidence_")
            or not _SAFE_ID.fullmatch(evidence_id)
            for evidence_id in value
        ):
            raise ValueError("adapter evidence_ids contains an invalid ID")
        if len(set(value)) != len(value):
            raise ValueError("adapter evidence_ids contains duplicates")
        return tuple(value)

    def _require_evidence_ids(self, scan_id: str, evidence_ids: Sequence[str]) -> None:
        unique_ids = tuple(dict.fromkeys(evidence_ids))
        for offset in range(0, len(unique_ids), _REFERENCE_QUERY_BATCH):
            batch = unique_ids[offset : offset + _REFERENCE_QUERY_BATCH]
            placeholders = ",".join("?" for _ in batch)
            rows = self._connection.execute(
                f"""
                SELECT evidence_id
                FROM evidence
                WHERE scan_id = ? AND evidence_id IN ({placeholders})
                """,
                (scan_id, *batch),
            ).fetchall()
            found = {str(row["evidence_id"]) for row in rows}
            missing = [evidence_id for evidence_id in batch if evidence_id not in found]
            if missing:
                raise KeyError(
                    f"evidence references are not part of scan {scan_id}: {missing}"
                )

    def latest_scan_id(self) -> str | None:
        row = self._connection.execute(
            "SELECT scan_id FROM scans ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["scan_id"])

    def integrity_check(self) -> bool:
        row = self._connection.execute("PRAGMA integrity_check").fetchone()
        return row is not None and row[0] == "ok"

    def schema_version(self) -> int:
        return self._read_schema_version()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
