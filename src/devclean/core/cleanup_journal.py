"""Durable, private intent journal for post-scan cleanup execution.

The journal never performs a filesystem mutation. It records intent before a
mutation and persists every state transition with ``synchronous=FULL``. It has
no staging, restore, or replay workflow.
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

JOURNAL_SCHEMA_VERSION = 1
MAX_JOURNAL_ERROR_LENGTH = 4_096
MAX_RETAINED_BATCHES = 128
_MAX_UINT64 = (1 << 64) - 1


class CleanupJournalError(RuntimeError):
    """The durable cleanup journal rejected an operation."""


class CleanupMode(StrEnum):
    RECYCLE = "RECYCLE"
    PERMANENT = "PERMANENT"


class JournalTargetKind(StrEnum):
    """Whether a journaled action covers one file or one whole subtree."""

    FILE = "FILE"
    DIRECTORY = "DIRECTORY"


class ActionState(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    EXECUTING = "EXECUTING"
    RECYCLED = "RECYCLED"
    PURGED = "PURGED"
    PURGE_PENDING = "PURGE_PENDING"
    FAILED_UNCHANGED = "FAILED_UNCHANGED"
    UNKNOWN = "UNKNOWN"


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
    scan_root_snapshot: ExactFileSnapshot
    category: str
    snapshot: ExactFileSnapshot
    target_kind: JournalTargetKind = JournalTargetKind.FILE
    subtree_files: int = 0
    subtree_bytes: int = 0


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
    scan_root_snapshot: ExactFileSnapshot
    category: str
    snapshot: ExactFileSnapshot
    last_error: str | None
    created_at: str
    updated_at: str
    target_kind: JournalTargetKind = JournalTargetKind.FILE
    subtree_files: int = 0
    subtree_bytes: int = 0


_SCHEMA = f"""
CREATE TABLE cleanup_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
INSERT INTO cleanup_meta(key, value) VALUES ('schema_version', '{JOURNAL_SCHEMA_VERSION}');

CREATE TABLE cleanup_batches (
    batch_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('RECYCLE', 'PERMANENT')),
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
    mode TEXT NOT NULL CHECK(mode IN ('RECYCLE', 'PERMANENT')),
    state TEXT NOT NULL CHECK(state IN (
        'INTENT_RECORDED', 'EXECUTING', 'RECYCLED', 'PURGED',
        'PURGE_PENDING', 'FAILED_UNCHANGED', 'UNKNOWN'
    )),
    source_path TEXT NOT NULL,
    scan_root TEXT NOT NULL,
    category TEXT NOT NULL,
    logical_size INTEGER NOT NULL CHECK(logical_size >= 0),
    volume_serial_u64 TEXT NOT NULL CHECK(
        volume_serial_u64 = '0'
        OR (
            length(volume_serial_u64) BETWEEN 1 AND 20
            AND volume_serial_u64 NOT GLOB '*[^0-9]*'
            AND substr(volume_serial_u64, 1, 1) BETWEEN '1' AND '9'
            AND (
                length(volume_serial_u64) < 20
                OR volume_serial_u64 <= '{_MAX_UINT64}'
            )
        )
    ),
    file_id TEXT NOT NULL,
    file_id_kind TEXT NOT NULL,
    link_count INTEGER NOT NULL CHECK(link_count = 1),
    attributes INTEGER,
    reparse_tag INTEGER,
    creation_time_ns INTEGER NOT NULL CHECK(creation_time_ns >= 0),
    last_write_time_ns INTEGER NOT NULL CHECK(last_write_time_ns >= 0),
    root_volume_serial_u64 TEXT NOT NULL CHECK(
        root_volume_serial_u64 = '0'
        OR (
            length(root_volume_serial_u64) BETWEEN 1 AND 20
            AND root_volume_serial_u64 NOT GLOB '*[^0-9]*'
            AND substr(root_volume_serial_u64, 1, 1) BETWEEN '1' AND '9'
            AND (
                length(root_volume_serial_u64) < 20
                OR root_volume_serial_u64 <= '{_MAX_UINT64}'
            )
        )
    ),
    root_file_id TEXT NOT NULL,
    root_file_id_kind TEXT NOT NULL,
    root_attributes INTEGER,
    root_reparse_tag INTEGER,
    root_creation_time_ns INTEGER NOT NULL CHECK(root_creation_time_ns >= 0),
    root_last_write_time_ns INTEGER NOT NULL CHECK(root_last_write_time_ns >= 0),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK(target_kind IN ('FILE', 'DIRECTORY')),
    subtree_files INTEGER NOT NULL CHECK(subtree_files >= 0),
    subtree_bytes INTEGER NOT NULL CHECK(subtree_bytes >= 0),
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
        self.path = Path(path or (data_dir() / "state" / "cleanup-journal.db"))
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
        # A directory intent's snapshot describes the directory entry, which is
        # zero bytes.  The durable batch total must state what the batch really
        # covers, so a whole-tree intent contributes its subtree total.
        logical_bytes = sum(
            intent.subtree_bytes
            if intent.target_kind is JournalTargetKind.DIRECTORY
            else intent.snapshot.logical_size
            for intent in intents
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prune_batches(
                    connection,
                    retain=MAX_RETAINED_BATCHES - 1,
                )
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
                            source_path, scan_root, category,
                            logical_size, volume_serial_u64,
                            file_id, file_id_kind, link_count,
                            attributes, reparse_tag, creation_time_ns, last_write_time_ns,
                            root_volume_serial_u64,
                            root_file_id, root_file_id_kind,
                            root_attributes, root_reparse_tag, root_creation_time_ns,
                            root_last_write_time_ns, last_error, created_at, updated_at,
                            target_kind, subtree_files, subtree_bytes
                        ) VALUES (
                            :action_id, :batch_id, :candidate_id, :action_ordinal, :mode, :state,
                            :source_path, :scan_root, :category,
                            :logical_size, :volume_serial_u64,
                            :file_id, :file_id_kind, :link_count,
                            :attributes, :reparse_tag, :creation_time_ns, :last_write_time_ns,
                            :root_volume_serial_u64,
                            :root_file_id, :root_file_id_kind,
                            :root_attributes, :root_reparse_tag, :root_creation_time_ns,
                            :root_last_write_time_ns, NULL, :created_at, :updated_at,
                            :target_kind, :subtree_files, :subtree_bytes
                        )""",
                        {
                            "target_kind": intent.target_kind.value,
                            "subtree_files": intent.subtree_files,
                            "subtree_bytes": intent.subtree_bytes,
                            "action_id": intent.action_id,
                            "batch_id": batch_id,
                            "candidate_id": intent.candidate_id,
                            "action_ordinal": ordinal,
                            "mode": mode.value,
                            "state": ActionState.INTENT_RECORDED.value,
                            "source_path": intent.source_path,
                            "scan_root": intent.scan_root,
                            "category": intent.category,
                            "logical_size": snapshot.logical_size,
                            "volume_serial_u64": _encode_volume_serial(
                                snapshot.volume_serial
                            ),
                            "file_id": snapshot.file_id,
                            "file_id_kind": snapshot.file_id_kind,
                            "link_count": snapshot.link_count,
                            "attributes": snapshot.attributes,
                            "reparse_tag": snapshot.reparse_tag,
                            "creation_time_ns": snapshot.creation_time_ns,
                            "last_write_time_ns": snapshot.last_write_time_ns,
                            "root_volume_serial_u64": _encode_volume_serial(
                                intent.scan_root_snapshot.volume_serial
                            ),
                            "root_file_id": intent.scan_root_snapshot.file_id,
                            "root_file_id_kind": (
                                intent.scan_root_snapshot.file_id_kind
                            ),
                            "root_attributes": intent.scan_root_snapshot.attributes,
                            "root_reparse_tag": intent.scan_root_snapshot.reparse_tag,
                            "root_creation_time_ns": (
                                intent.scan_root_snapshot.creation_time_ns
                            ),
                            "root_last_write_time_ns": (
                                intent.scan_root_snapshot.last_write_time_ns
                            ),
                            "created_at": now,
                            "updated_at": now,
                        },
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
                ActionState.UNKNOWN,
                ActionState.PURGE_PENDING,
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
            self._prune_batches(connection)
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

    def actions_for_batch(self, batch_id: str) -> tuple[JournalAction, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM cleanup_actions
                   WHERE batch_id = ? ORDER BY action_ordinal""",
                (batch_id,),
            ).fetchall()
        return tuple(_row_to_action(row) for row in rows)

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
            if version is None:
                raise CleanupJournalError("cleanup journal schema version is unsupported")
            if str(version[0]) != str(JOURNAL_SCHEMA_VERSION):
                raise CleanupJournalError("cleanup journal schema version is unsupported")
            connection.execute("BEGIN IMMEDIATE")
            self._prune_batches(connection)
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
    def _prune_batches(
        connection: sqlite3.Connection,
        *,
        retain: int = MAX_RETAINED_BATCHES,
    ) -> None:
        """Keep the entire journal bounded, including unresolved outcomes."""

        rows = connection.execute(
            """SELECT batch_id FROM cleanup_batches
               ORDER BY updated_at DESC, batch_id DESC
               LIMIT -1 OFFSET ?""",
            (retain,),
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
                "DELETE FROM cleanup_batches WHERE batch_id = ?",
                (batch_id,),
            )


def _row_to_action(row: sqlite3.Row) -> JournalAction:
    snapshot = ExactFileSnapshot(
        logical_size=int(row["logical_size"]),
        volume_serial=_decode_volume_serial(row["volume_serial_u64"]),
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
        volume_serial=_decode_volume_serial(row["root_volume_serial_u64"]),
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
        scan_root_snapshot=root_snapshot,
        category=str(row["category"]),
        snapshot=snapshot,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        target_kind=JournalTargetKind(str(row["target_kind"])),
        subtree_files=int(row["subtree_files"]),
        subtree_bytes=int(row["subtree_bytes"]),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:MAX_JOURNAL_ERROR_LENGTH]


def _encode_volume_serial(value: int) -> str:
    if value < 0 or value > _MAX_UINT64:
        raise CleanupJournalError("volume serial is outside the unsigned 64-bit range")
    return str(value)


def _decode_volume_serial(value: object) -> int:
    if not isinstance(value, str):
        raise CleanupJournalError("stored volume serial is not canonical decimal text")
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        raise CleanupJournalError("stored volume serial is not canonical decimal text")
    decoded = int(value)
    if decoded > _MAX_UINT64:
        raise CleanupJournalError("stored volume serial is outside the unsigned 64-bit range")
    return decoded


__all__ = [
    "MAX_RETAINED_BATCHES",
    "ActionState",
    "BatchState",
    "CleanupIntent",
    "CleanupJournal",
    "CleanupJournalError",
    "CleanupMode",
    "JournalAction",
    "JournalTargetKind",
]
