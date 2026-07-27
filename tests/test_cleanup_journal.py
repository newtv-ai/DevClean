from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from devclean.core.cleanup_journal import (
    MAX_RETAINED_BATCHES,
    ActionState,
    CleanupIntent,
    CleanupJournal,
    CleanupMode,
)
from devclean.platform.windows.exact_cleanup import ExactFileSnapshot


def _snapshot(value: int) -> ExactFileSnapshot:
    return ExactFileSnapshot(
        logical_size=value,
        volume_serial=1,
        file_id=f"{value:032x}",
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0,
        reparse_tag=None,
        creation_time_ns=1,
        last_write_time_ns=2,
    )


def test_journal_records_transitions_and_bounds_all_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devclean.core.cleanup_journal as journal_module

    monkeypatch.setattr(journal_module, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(journal_module, "secure_private_directory", lambda _path: None)
    monkeypatch.setattr(journal_module, "secure_private_file", lambda _path: None)
    path = tmp_path / "journal.db"
    journal = CleanupJournal(path)
    root_snapshot = _snapshot(0)

    for index in range(MAX_RETAINED_BATCHES + 2):
        action_id = f"action-{index}"
        journal.record_batch(
            f"batch-{index:03d}",
            CleanupMode.PERMANENT,
            (
                CleanupIntent(
                    action_id=action_id,
                    candidate_id=f"candidate-{index}",
                    source_path=rf"G:\scan\{index}.tmp",
                    scan_root=r"G:\scan",
                    scan_root_snapshot=root_snapshot,
                    category="OTHER",
                    snapshot=_snapshot(index + 1),
                ),
            ),
        )
        journal.transition(
            action_id,
            expected=(ActionState.INTENT_RECORDED,),
            new_state=ActionState.EXECUTING,
        )
        journal.transition(
            action_id,
            expected=(ActionState.EXECUTING,),
            new_state=ActionState.PURGE_PENDING,
        )
        journal.transition(
            action_id,
            expected=(ActionState.PURGE_PENDING,),
            new_state=ActionState.PURGED,
        )
        journal.finalize_batch(f"batch-{index:03d}")

    with sqlite3.connect(path) as connection:
        batches = connection.execute("SELECT COUNT(*) FROM cleanup_batches").fetchone()
        actions = connection.execute("SELECT COUNT(*) FROM cleanup_actions").fetchone()
        events = connection.execute("SELECT COUNT(*) FROM cleanup_events").fetchone()

    assert batches == (MAX_RETAINED_BATCHES,)
    assert actions == (MAX_RETAINED_BATCHES,)
    assert events == (MAX_RETAINED_BATCHES * 4,)
