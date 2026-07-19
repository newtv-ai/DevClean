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


def _snapshot(file_id: str = "ab" * 16) -> ExactFileSnapshot:
    return ExactFileSnapshot(
        logical_size=7,
        volume_serial=42,
        file_id=file_id,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=32,
        reparse_tag=None,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _root_snapshot() -> ExactFileSnapshot:
    return ExactFileSnapshot(
        logical_size=0,
        volume_serial=42,
        file_id="10" * 16,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=16,
        reparse_tag=None,
        creation_time_ns=10,
        last_write_time_ns=20,
    )


def _intent(tmp_path: Path, *, action: str = "action_a") -> CleanupIntent:
    return CleanupIntent(
        action_id=action,
        candidate_id="candidate_" + action,
        source_path=str(tmp_path / f"{action}.bin"),
        scan_root=str(tmp_path),
        approved_root=str(tmp_path),
        approved_root_snapshot=_root_snapshot(),
        quarantine_path=str(tmp_path / ".DevClean-quarantine-v1" / action),
        category="OTHER",
        snapshot=_snapshot(),
    )


def test_journal_uses_single_file_delete_mode_and_full_sync(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    with closing(sqlite3.connect(journal.path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert audit_private_directory(journal.path.parent).policy_satisfied
    assert audit_private_file(journal.path).policy_satisfied


def test_record_batch_persists_intent_and_approved_root_identity(tmp_path: Path) -> None:
    journal = CleanupJournal(tmp_path / "state" / "cleanup.db")
    journal.record_batch("batch_a", CleanupMode.RECYCLE, (_intent(tmp_path),))
    action = journal.action("action_a")
    assert action.state is ActionState.INTENT_RECORDED
    assert action.approved_root == str(tmp_path)
    assert action.approved_root_snapshot.file_id == "10" * 16
    assert journal.event_states("action_a") == ("INTENT_RECORDED",)
    assert journal.finalize_batch("batch_a") is BatchState.NEEDS_REVIEW


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
