"""Durable, private intent journal for post-scan cleanup execution.

The journal never performs a filesystem mutation.  It records intent before a
mutation, persists every state transition with ``synchronous=FULL``, and leaves
ambiguous crash states for reconciliation.  It deliberately has no replay API.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from devclean.core.paths import data_dir
from devclean.platform.windows.exact_cleanup import ExactFileSnapshot
from devclean.platform.windows.security import secure_private_directory, secure_private_file
from devclean.platform.windows.volumes import is_local_fixed_path

JOURNAL_SCHEMA_VERSION = 2
MAX_JOURNAL_ERROR_LENGTH = 4_096
MAX_RETAINED_COMPLETED_BATCHES = 128


class CleanupJournalError(RuntimeError):
    """The durable cleanup journal rejected an operation."""


class CleanupMode(StrEnum):
    RECYCLE = "RECYCLE"
    PERMANENT = "PERMANENT"
    CONFIRMED_PURGE = "CONFIRMED_PURGE"


class ActionState(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    EXECUTING = "EXECUTING"
    QUARANTINED = "QUARANTINED"
    # RECYCLE_PENDING/RECYCLED are retained for schema-v2 compatibility with
    # journals written before the Shell Recycle Bin bridge was withdrawn; the
    # current runtime never emits them, and reconciliation still treats
    # RECYCLE_PENDING as ambiguous (NEEDS_REVIEW), never as a success state.
    RECYCLE_PENDING = "RECYCLE_PENDING"
    RECYCLED = "RECYCLED"
    PURGED = "PURGED"
    PURGE_PENDING = "PURGE_PENDING"
    FAILED_UNCHANGED = "FAILED_UNCHANGED"
    UNKNOWN = "UNKNOWN"
    RESTORE_INTENT = "RESTORE_INTENT"
    RESTORING = "RESTORING"
    RESTORED = "RESTORED"


class BatchState(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True, slots=True)
class CleanupIntent:
    action_id: str
    candidate_id: str
    source_path: str
    scan_root: str
    approved_root: str
    approved_root_snapshot: ExactFileSnapshot
    quarantine_path: str | None
    category: str
    snapshot: ExactFileSnapshot


@dataclass(frozen=True, slots=True)
class JournalAction:
    action_id: str
    batch_id: str
    candidate_id: str
    action_ordinal: int
    mode: CleanupMode
    state: ActionState
    source_path: str
    scan_root: str
    approved_root: str
    approved_root_snapshot: ExactFileSnapshot
    quarantine_path: str | None
    category: str
    snapshot: ExactFileSnapshot
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JournalBatch:
    batch_id: str
    mode: CleanupMode
    state: BatchState
    action_count: int
    logical_bytes: int
    created_at: str
    updated_at: str


_SCHEMA = f"""
CREATE TABLE cleanup_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
INSERT INTO cleanup_meta(key, value) VALUES ('schema_version', '{JOURNAL_SCHEMA_VERSION}');

CREATE TABLE cleanup_batches (
    batch_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('RECYCLE', 'PERMANENT', 'CONFIRMED_PURGE')),
    state TEXT NOT NULL CHECK(state IN ('ACTIVE', 'COMPLETED', 'NEEDS_REVIEW')),
    action_count INTEGER NOT NULL CHECK(action_count BETWEEN 1 AND 32),
    logical_bytes INTEGER NOT NULL CHECK(logical_bytes >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE cleanup_actions (
    action_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES cleanup_batches(batch_id),
    candidate_id TEXT NOT NULL,
    action_ordinal INTEGER NOT NULL CHECK(action_ordinal >= 0),
    mode TEXT NOT NULL CHECK(mode IN ('RECYCLE', 'PERMANENT', 'CONFIRMED_PURGE')),
    state TEXT NOT NULL CHECK(state IN (
        'INTENT_RECORDED', 'EXECUTING', 'QUARANTINED', 'RECYCLE_PENDING',
        'RECYCLED', 'PURGED', 'PURGE_PENDING', 'FAILED_UNCHANGED', 'UNKNOWN',
        'RESTORE_INTENT', 'RESTORING', 'RESTORED'
    )),
    source_path TEXT NOT NULL,
    scan_root TEXT NOT NULL,
    approved_root TEXT NOT NULL,
    quarantine_path TEXT,
    category TEXT NOT NULL,
    logical_size INTEGER NOT NULL CHECK(logical_size >= 0),
    volume_serial INTEGER NOT NULL CHECK(volume_serial >= 0),
    file_id TEXT NOT NULL,
    file_id_kind TEXT NOT NULL,
    link_count INTEGER NOT NULL CHECK(link_count = 1),
    attributes INTEGER,
    reparse_tag INTEGER,
    creation_time_ns INTEGER NOT NULL CHECK(creation_time_ns >= 0),
    last_write_time_ns INTEGER NOT NULL CHECK(last_write_time_ns >= 0),
    root_volume_serial INTEGER NOT NULL CHECK(root_volume_serial >= 0),
    root_file_id TEXT NOT NULL,
    root_file_id_kind TEXT NOT NULL,
    root_attributes INTEGER,
    root_reparse_tag INTEGER,
    root_creation_time_ns INTEGER NOT NULL CHECK(root_creation_time_ns >= 0),
    root_last_write_time_ns INTEGER NOT NULL CHECK(root_last_write_time_ns >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, candidate_id),
    UNIQUE(batch_id, source_path)
) STRICT;

CREATE INDEX cleanup_actions_batch ON cleanup_actions(batch_id, action_id);
CREATE INDEX cleanup_actions_state ON cleanup_actions(state, updated_at);

CREATE TABLE cleanup_events (
    event_id INTEGER PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES cleanup_actions(action_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
) STRICT;
"""


class CleanupJournal:
    """SQLite intent log with explicit compare-and-transition operations."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or (data_dir() / "state" / "cleanup-intents-v1.db"))
        if not self.path.is_absolute():
            raise CleanupJournalError("cleanup journal path must be absolute")
        if not is_local_fixed_path(self.path.parent):
            raise CleanupJournalError("cleanup journal must be on a local fixed volume")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        secure_private_directory(self.path.parent)
        self._initialize()

    def record_batch(
        self,
        batch_id: str,
        mode: CleanupMode,
        intents: Sequence[CleanupIntent],
    ) -> None:
        """Durably record every action before any target mutation."""

        if not intents or len(intents) > 32:
            raise CleanupJournalError("a cleanup batch must contain between 1 and 32 actions")
        now = _now()
        logical_bytes = sum(intent.snapshot.logical_size for intent in intents)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prune_completed_batches(connection)
                connection.execute(
                    """INSERT INTO cleanup_batches
                       (batch_id, mode, state, action_count, logical_bytes, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        batch_id,
                        mode.value,
                        BatchState.ACTIVE.value,
                        len(intents),
                        logical_bytes,
                        now,
                        now,
                    ),
                )
                for ordinal, intent in enumerate(intents):
                    snapshot = intent.snapshot
                    connection.execute(
                        """INSERT INTO cleanup_actions (
                            action_id, batch_id, candidate_id, action_ordinal, mode, state,
                            source_path,
                            scan_root, approved_root, quarantine_path, category,
                            logical_size, volume_serial, file_id, file_id_kind, link_count,
                            attributes, reparse_tag, creation_time_ns, last_write_time_ns,
                            root_volume_serial, root_file_id, root_file_id_kind,
                            root_attributes, root_reparse_tag, root_creation_time_ns,
                            root_last_write_time_ns, last_error, created_at, updated_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?
                        )""",
                        (
                            intent.action_id,
                            batch_id,
                            intent.candidate_id,
                            ordinal,
                            mode.value,
                            ActionState.INTENT_RECORDED.value,
                            intent.source_path,
                            intent.scan_root,
                            intent.approved_root,
                            intent.quarantine_path,
                            intent.category,
                            snapshot.logical_size,
                            snapshot.volume_serial,
                            snapshot.file_id,
                            snapshot.file_id_kind,
                            snapshot.link_count,
                            snapshot.attributes,
                            snapshot.reparse_tag,
                            snapshot.creation_time_ns,
                            snapshot.last_write_time_ns,
                            intent.approved_root_snapshot.volume_serial,
                            intent.approved_root_snapshot.file_id,
                            intent.approved_root_snapshot.file_id_kind,
                            intent.approved_root_snapshot.attributes,
                            intent.approved_root_snapshot.reparse_tag,
                            intent.approved_root_snapshot.creation_time_ns,
                            intent.approved_root_snapshot.last_write_time_ns,
                            now,
                            now,
                        ),
                    )
                    self._event(
                        connection,
                        intent.action_id,
                        None,
                        ActionState.INTENT_RECORDED,
                        "durable intent recorded before target mutation",
                        now,
                    )
                connection.commit()
        except (sqlite3.Error, CleanupJournalError) as error:
            raise CleanupJournalError(f"could not record cleanup intent: {error}") from error

    def transition(
        self,
        action_id: str,
        *,
        expected: Iterable[ActionState],
        new_state: ActionState,
        detail: str | None = None,
        error: str | None = None,
    ) -> None:
        """Atomically transition only from one explicitly expected state."""

        expected_values = tuple(state.value for state in expected)
        if not expected_values:
            raise CleanupJournalError("at least one expected action state is required")
        bounded_error = _bounded(error)
        bounded_detail = _bounded(detail)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, batch_id FROM cleanup_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None:
                raise CleanupJournalError("cleanup action does not exist")
            old = ActionState(str(row[0]))
            if old.value not in expected_values:
                raise CleanupJournalError(
                    f"cleanup action is {old.value}, expected one of {sorted(expected_values)}"
                )
            connection.execute(
                """UPDATE cleanup_actions
                   SET state = ?, last_error = ?, updated_at = ?
                   WHERE action_id = ?""",
                (new_state.value, bounded_error, now, action_id),
            )
            self._event(connection, action_id, old, new_state, bounded_detail, now)
            connection.execute(
                "UPDATE cleanup_batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                (BatchState.ACTIVE.value, now, str(row[1])),
            )
            connection.commit()

    def finalize_batch(self, batch_id: str) -> BatchState:
        """Summarize a batch without replaying an incomplete action."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT state FROM cleanup_actions WHERE batch_id = ?", (batch_id,)
            ).fetchall()
            if not rows:
                raise CleanupJournalError("cleanup batch does not exist")
            states = {ActionState(str(row[0])) for row in rows}
            ambiguous = {
                ActionState.INTENT_RECORDED,
                ActionState.EXECUTING,
                ActionState.RECYCLE_PENDING,
                ActionState.UNKNOWN,
                ActionState.QUARANTINED,
                ActionState.PURGE_PENDING,
                ActionState.RESTORE_INTENT,
                ActionState.RESTORING,
            }
            state = (
                BatchState.NEEDS_REVIEW
                if states & ambiguous
                else BatchState.COMPLETED
            )
            connection.execute(
                "UPDATE cleanup_batches SET state = ?, updated_at = ? WHERE batch_id = ?",
                (state.value, _now(), batch_id),
            )
            self._prune_completed_batches(connection)
            connection.commit()
        return state

    def action(self, action_id: str) -> JournalAction:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cleanup_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
        if row is None:
            raise CleanupJournalError("cleanup action does not exist")
        return _row_to_action(row)

    def batch(self, batch_id: str) -> JournalBatch:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cleanup_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            raise CleanupJournalError("cleanup batch does not exist")
        return JournalBatch(
            batch_id=str(row["batch_id"]),
            mode=CleanupMode(str(row["mode"])),
            state=BatchState(str(row["state"])),
            action_count=int(row["action_count"]),
            logical_bytes=int(row["logical_bytes"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def actions_for_batch(self, batch_id: str) -> tuple[JournalAction, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM cleanup_actions
                   WHERE batch_id = ? ORDER BY action_ordinal""",
                (batch_id,),
            ).fetchall()
        return tuple(_row_to_action(row) for row in rows)

    def unresolved_actions(self) -> tuple[JournalAction, ...]:
        unresolved = tuple(
            state.value
            for state in (
                ActionState.INTENT_RECORDED,
                ActionState.EXECUTING,
                ActionState.QUARANTINED,
                ActionState.RECYCLE_PENDING,
                ActionState.PURGE_PENDING,
                ActionState.UNKNOWN,
                ActionState.RESTORE_INTENT,
                ActionState.RESTORING,
            )
        )
        placeholders = ",".join("?" for _ in unresolved)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM cleanup_actions WHERE state IN ({placeholders}) "
                "ORDER BY updated_at, action_id",
                unresolved,
            ).fetchall()
        return tuple(_row_to_action(row) for row in rows)

    def event_states(self, action_id: str) -> tuple[str, ...]:
        """Return an immutable audit projection useful to tests and support."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT to_state FROM cleanup_events WHERE action_id = ? ORDER BY event_id",
                (action_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _initialize(self) -> None:
        exists = self.path.exists()
        with self._connect(create=True) as connection:
            if not exists:
                # New journals reclaim deleted history on commit. The database
                # stores cleanup safety state, never a full scan inventory.
                connection.execute("PRAGMA auto_vacuum = FULL")
                connection.executescript(_SCHEMA)
                connection.commit()
            version = connection.execute(
                "SELECT value FROM cleanup_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is None or int(version[0]) != JOURNAL_SCHEMA_VERSION:
                raise CleanupJournalError("cleanup journal schema version is unsupported")
            connection.execute("BEGIN IMMEDIATE")
            self._prune_completed_batches(connection)
            connection.commit()
        secure_private_file(self.path)

    @contextmanager
    def _connect(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        if not create and not self.path.is_file():
            raise CleanupJournalError("cleanup journal is missing")
        try:
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        except sqlite3.Error as error:
            raise CleanupJournalError(
                f"could not open cleanup journal database: {error}"
            ) from error
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            # A cleanup intent must not depend on a separate WAL file surviving
            # a crash/copy boundary. DELETE + FULL makes the single database
            # file the durable recovery artifact required by the execution ADR.
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA busy_timeout = 10000")
            yield connection
        except sqlite3.Error as error:
            raise CleanupJournalError(f"cleanup journal database error: {error}") from error
        finally:
            connection.close()

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        action_id: str,
        old: ActionState | None,
        new: ActionState,
        detail: str | None,
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO cleanup_events
               (action_id, from_state, to_state, detail, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (action_id, old.value if old else None, new.value, detail, now),
        )

    @staticmethod
    def _prune_completed_batches(connection: sqlite3.Connection) -> None:
        """Bound terminal history while preserving every unresolved action."""

        rows = connection.execute(
            """SELECT batch_id FROM cleanup_batches
               WHERE state = ?
               ORDER BY updated_at DESC, batch_id DESC
               LIMIT -1 OFFSET ?""",
            (BatchState.COMPLETED.value, MAX_RETAINED_COMPLETED_BATCHES),
        ).fetchall()
        for row in rows:
            batch_id = str(row[0])
            connection.execute(
                """DELETE FROM cleanup_events
                   WHERE action_id IN (
                       SELECT action_id FROM cleanup_actions WHERE batch_id = ?
                   )""",
                (batch_id,),
            )
            connection.execute(
                "DELETE FROM cleanup_actions WHERE batch_id = ?",
                (batch_id,),
            )
            connection.execute(
                "DELETE FROM cleanup_batches WHERE batch_id = ? AND state = ?",
                (batch_id, BatchState.COMPLETED.value),
            )


def _row_to_action(row: sqlite3.Row) -> JournalAction:
    snapshot = ExactFileSnapshot(
        logical_size=int(row["logical_size"]),
        volume_serial=int(row["volume_serial"]),
        file_id=str(row["file_id"]),
        file_id_kind=str(row["file_id_kind"]),
        link_count=int(row["link_count"]),
        attributes=int(row["attributes"]) if row["attributes"] is not None else None,
        reparse_tag=int(row["reparse_tag"]) if row["reparse_tag"] is not None else None,
        creation_time_ns=int(row["creation_time_ns"]),
        last_write_time_ns=int(row["last_write_time_ns"]),
    )
    root_snapshot = ExactFileSnapshot(
        logical_size=0,
        volume_serial=int(row["root_volume_serial"]),
        file_id=str(row["root_file_id"]),
        file_id_kind=str(row["root_file_id_kind"]),
        link_count=1,
        attributes=(
            int(row["root_attributes"]) if row["root_attributes"] is not None else None
        ),
        reparse_tag=(
            int(row["root_reparse_tag"]) if row["root_reparse_tag"] is not None else None
        ),
        creation_time_ns=int(row["root_creation_time_ns"]),
        last_write_time_ns=int(row["root_last_write_time_ns"]),
    )
    return JournalAction(
        action_id=str(row["action_id"]),
        batch_id=str(row["batch_id"]),
        candidate_id=str(row["candidate_id"]),
        action_ordinal=int(row["action_ordinal"]),
        mode=CleanupMode(str(row["mode"])),
        state=ActionState(str(row["state"])),
        source_path=str(row["source_path"]),
        scan_root=str(row["scan_root"]),
        approved_root=str(row["approved_root"]),
        approved_root_snapshot=root_snapshot,
        quarantine_path=(
            str(row["quarantine_path"]) if row["quarantine_path"] is not None else None
        ),
        category=str(row["category"]),
        snapshot=snapshot,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_JOURNAL_ERROR_LENGTH]


__all__ = [
    "MAX_RETAINED_COMPLETED_BATCHES",
    "ActionState",
    "BatchState",
    "CleanupIntent",
    "CleanupJournal",
    "CleanupJournalError",
    "CleanupMode",
    "JournalAction",
    "JournalBatch",
]
