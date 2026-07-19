from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import devclean.core.cleanup_journal as cleanup_journal_module
from devclean.core.cleanup_journal import (
    ActionState,
    BatchState,
    CleanupIntent,
    CleanupJournal,
    CleanupJournalError,
    CleanupMode,
)
from devclean.platform.windows.exact_cleanup import ExactFileSnapshot
from devclean.platform.windows.security import audit_private_directory, audit_private_file


def _snapshot(
    file_id: str = "ab" * 16,
    *,
    volume_serial: int = 42,
) -> ExactFileSnapshot:
    return ExactFileSnapshot(
        logical_size=7,
        volume_serial=volume_serial,
        file_id=file_id,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=32,
        reparse_tag=None,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _root_snapshot(*, volume_serial: int = 42) -> ExactFileSnapshot:
    return ExactFileSnapshot(
        logical_size=0,
        volume_serial=volume_serial,
        file_id="10" * 16,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=16,
        reparse_tag=None,
        creation_time_ns=10,
        last_write_time_ns=20,
    )


def _intent(
    tmp_path: Path,
    *,
    action: str = "action_a",
    volume_serial: int = 42,
    root_volume_serial: int = 42,
) -> CleanupIntent:
    return CleanupIntent(
        action_id=action,
        candidate_id="candidate_" + action,
        source_path=str(tmp_path / f"{action}.bin"),
        scan_root=str(tmp_path),
        approved_root=str(tmp_path),
        approved_root_snapshot=_root_snapshot(volume_serial=root_volume_serial),
        quarantine_path=str(tmp_path / ".DevClean-quarantine-v1" / action),
        category="OTHER",
        snapshot=_snapshot(volume_serial=volume_serial),
    )


def test_journal_uses_single_file_delete_mode_and_full_sync(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    with closing(sqlite3.connect(journal.path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert audit_private_directory(journal.path.parent).policy_satisfied
    assert audit_private_file(journal.path).policy_satisfied


def test_journal_rejects_relative_or_nonlocal_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(CleanupJournalError, match="must be absolute"):
        CleanupJournal(Path("relative-cleanup.db"))

    monkeypatch.setattr(
        cleanup_journal_module,
        "is_local_fixed_path",
        lambda path: False,
    )
    with pytest.raises(CleanupJournalError, match="local fixed volume"):
        CleanupJournal(tmp_path / "state" / "cleanup.db")


def test_record_batch_persists_intent_and_approved_root_identity(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    action = journal.action("action_a")
    assert action.state is ActionState.INTENT_RECORDED
    assert action.approved_root == str(tmp_path)
    assert action.approved_root_snapshot.file_id == "10" * 16
    assert journal.event_states("action_a") == ("INTENT_RECORDED",)
    assert journal.finalize_batch("batch_a") is BatchState.NEEDS_REVIEW


def test_volume_serials_round_trip_across_unsigned_64_bit_range(
    tmp_path: Path,
) -> None:
    source_serial = (1 << 64) - 1
    root_serial = (1 << 63) + 123
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch(
        "batch_u64",
        CleanupMode.RECYCLE,
        (
            _intent(
                tmp_path,
                volume_serial=source_serial,
                root_volume_serial=root_serial,
            ),
        ),
    )

    action = journal.action("action_a")
    assert action.snapshot.volume_serial == source_serial
    assert action.approved_root_snapshot.volume_serial == root_serial
    with closing(sqlite3.connect(journal.path)) as connection:
        stored = connection.execute(
            """SELECT volume_serial, volume_serial_u64,
                      root_volume_serial, root_volume_serial_u64
               FROM cleanup_actions WHERE action_id = 'action_a'"""
        ).fetchone()
    assert stored == (0, str(source_serial), 0, str(root_serial))


def test_schema_v2_journal_migrates_without_losing_actions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cleanup.db"
    journal = CleanupJournal(path)
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "ALTER TABLE cleanup_actions DROP COLUMN volume_serial_u64"
        )
        connection.execute(
            "ALTER TABLE cleanup_actions DROP COLUMN root_volume_serial_u64"
        )
        connection.execute(
            "UPDATE cleanup_meta SET value = '2' WHERE key = 'schema_version'"
        )
        connection.commit()

    migrated = CleanupJournal(path)

    assert migrated.action("action_a").snapshot.volume_serial == 42
    with closing(sqlite3.connect(path)) as connection:
        version = connection.execute(
            "SELECT value FROM cleanup_meta WHERE key = 'schema_version'"
        ).fetchone()
        serials = connection.execute(
            """SELECT volume_serial_u64, root_volume_serial_u64
               FROM cleanup_actions WHERE action_id = 'action_a'"""
        ).fetchone()
    assert version == ("3",)
    assert serials == ("42", "42")


@pytest.mark.parametrize("version", [None, "invalid", "99"])
def test_journal_rejects_missing_malformed_or_unsupported_schema_version(
    tmp_path: Path,
    version: str | None,
) -> None:
    path = tmp_path / f"state-{version}" / "cleanup.db"
    CleanupJournal(path)
    with closing(sqlite3.connect(path)) as connection:
        if version is None:
            connection.execute(
                "DELETE FROM cleanup_meta WHERE key = 'schema_version'"
            )
        else:
            connection.execute(
                """UPDATE cleanup_meta SET value = ?
                   WHERE key = 'schema_version'""",
                (version,),
            )
        connection.commit()

    with pytest.raises(CleanupJournalError, match="schema version is unsupported"):
        CleanupJournal(path)


@pytest.mark.parametrize("volume_serial", [-1, 1 << 64])
def test_record_batch_rejects_volume_serial_outside_unsigned_64_bit_range(
    tmp_path: Path,
    volume_serial: int,
) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")

    with pytest.raises(CleanupJournalError, match="unsigned 64-bit range"):
        journal.record_batch(
            "batch_invalid",
            CleanupMode.RECYCLE,
            (_intent(tmp_path, volume_serial=volume_serial),),
        )
    with pytest.raises(CleanupJournalError, match="does not exist"):
        journal.batch("batch_invalid")


@pytest.mark.parametrize(
    "stored",
    [None, "", "01", str(1 << 64)],
)
def test_volume_serial_decoder_rejects_noncanonical_or_out_of_range_text(
    stored: object,
) -> None:
    with pytest.raises(CleanupJournalError, match="stored volume serial"):
        cleanup_journal_module._decode_volume_serial(stored)


def test_duplicate_batch_cannot_replay_an_intent(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    intent = _intent(tmp_path)
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (intent,))
    with pytest.raises(CleanupJournalError, match="could not record"):
        journal.record_batch("batch_a", CleanupMode.RECYCLE, (intent,))
    assert journal.event_states("action_a") == ("INTENT_RECORDED",)


def test_transition_is_compare_and_set(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    journal.transition(
        "action_a",
        expected=(ActionState.INTENT_RECORDED,),
        new_state=ActionState.EXECUTING,
    )
    with pytest.raises(CleanupJournalError, match="expected"):
        journal.transition(
            "action_a",
            expected=(ActionState.INTENT_RECORDED,),
            new_state=ActionState.EXECUTING,
        )
    assert journal.action("action_a").state is ActionState.EXECUTING


def test_missing_action_and_batch_operations_fail_closed(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")

    with pytest.raises(CleanupJournalError, match="expected action state"):
        journal.transition(
            "missing",
            expected=(),
            new_state=ActionState.EXECUTING,
        )
    with pytest.raises(CleanupJournalError, match="action does not exist"):
        journal.transition(
            "missing",
            expected=(ActionState.INTENT_RECORDED,),
            new_state=ActionState.EXECUTING,
        )
    with pytest.raises(CleanupJournalError, match="batch does not exist"):
        journal.finalize_batch("missing")
    with pytest.raises(CleanupJournalError, match="action does not exist"):
        journal.action("missing")

    journal.path.unlink()
    with pytest.raises(CleanupJournalError, match="journal is missing"):
        journal.action("missing")


def test_database_open_error_is_wrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")

    def fail_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("synthetic open failure")

    monkeypatch.setattr(cleanup_journal_module.sqlite3, "connect", fail_connect)

    with pytest.raises(CleanupJournalError, match="could not open"):
        journal.action("missing")


def test_unknown_is_visible_and_never_auto_replayed(tmp_path: Path) -> None:
    path = tmp_path / "state" / "cleanup.db"
    journal = CleanupJournal(path)
    journal.record_batch("batch_a", CleanupMode.PERMANENT, (_intent(tmp_path),))
    journal.transition(
        "action_a",
        expected=(ActionState.INTENT_RECORDED,),
        new_state=ActionState.EXECUTING,
    )
    journal.transition(
        "action_a",
        expected=(ActionState.EXECUTING,),
        new_state=ActionState.UNKNOWN,
        error="crash boundary",
    )
    reopened = CleanupJournal(path)
    unresolved = reopened.unresolved_actions()
    assert [action.state for action in unresolved] == [ActionState.UNKNOWN]
    assert reopened.event_states("action_a") == (
        "INTENT_RECORDED",
        "EXECUTING",
        "UNKNOWN",
    )
    assert reopened.finalize_batch("batch_a") is BatchState.NEEDS_REVIEW


def test_quarantined_action_needs_review_and_is_queryable_for_restore(
    tmp_path: Path,
) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    journal.transition(
        "action_a",
        expected=(ActionState.INTENT_RECORDED,),
        new_state=ActionState.EXECUTING,
    )
    journal.transition(
        "action_a",
        expected=(ActionState.EXECUTING,),
        new_state=ActionState.QUARANTINED,
    )
    assert journal.finalize_batch("batch_a") is BatchState.NEEDS_REVIEW
    assert [action.state for action in journal.unresolved_actions()] == [
        ActionState.QUARANTINED
    ]


def test_batch_limit_is_enforced_before_insert(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    intents = tuple(_intent(tmp_path, action=f"action_{index}") for index in range(33))
    with pytest.raises(CleanupJournalError, match="between 1 and 32"):
        journal.record_batch("batch_large", CleanupMode.RECYCLE, intents)


def test_error_text_is_bounded(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    journal.transition(
        "action_a",
        expected=(ActionState.INTENT_RECORDED,),
        new_state=ActionState.UNKNOWN,
        error="x" * 10_000,
    )
    assert len(journal.action("action_a").last_error or "") == 4_096


def test_completed_history_is_bounded_without_pruning_unresolved_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup_journal_module, "MAX_RETAINED_COMPLETED_BATCHES", 2)
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch(
        "needs_review",
        CleanupMode.RECYCLE,
        (_intent(tmp_path, action="unresolved"),),
    )

    for index in range(3):
        batch_id = f"completed_{index}"
        action_id = f"terminal_{index}"
        journal.record_batch(
            batch_id,
            CleanupMode.RECYCLE,
            (_intent(tmp_path, action=action_id),),
        )
        journal.transition(
            action_id,
            expected=(ActionState.INTENT_RECORDED,),
            new_state=ActionState.FAILED_UNCHANGED,
        )
        assert journal.finalize_batch(batch_id) is BatchState.COMPLETED

    with pytest.raises(CleanupJournalError, match="does not exist"):
        journal.batch("completed_0")
    assert journal.event_states("terminal_0") == ()
    assert journal.batch("completed_1").state is BatchState.COMPLETED
    assert journal.batch("completed_2").state is BatchState.COMPLETED
    assert [action.action_id for action in journal.unresolved_actions()] == ["unresolved"]


def test_new_cleanup_journal_uses_full_auto_vacuum(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    with closing(sqlite3.connect(journal.path)) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 1
