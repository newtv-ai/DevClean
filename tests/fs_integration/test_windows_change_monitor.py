from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from devclean.scanner.change_monitor import (
    ChangeAction,
    ChangeHint,
    DirectoryChangeMonitor,
    MonitorState,
)


def _wait_for_hint(
    monitor: DirectoryChangeMonitor,
    *,
    after_sequence: int,
    action: ChangeAction,
    name: str,
    timeout: float = 5.0,
) -> tuple[int, tuple[ChangeHint, ...]]:
    deadline = time.monotonic() + timeout
    last_hints: tuple[ChangeHint, ...] = ()
    while time.monotonic() < deadline:
        batch = monitor.read_changes(
            session_token=monitor.session_token,
            after_sequence=after_sequence,
        )
        assert batch.requires_full_scan is False, batch
        last_hints = batch.hints
        if any(
            hint.action is action and Path(hint.relative_path).name == name for hint in batch.hints
        ):
            return batch.through_sequence, batch.hints
        time.sleep(0.05)
    pytest.fail(f"missing {action.value} notification for {name}: {last_hints!r}")


@pytest.mark.skipif(os.name != "nt", reason="ReadDirectoryChangesW Windows canary")
def test_windows_monitor_observes_create_modify_rename_and_delete(
    tmp_path: Path,
) -> None:
    monitor = DirectoryChangeMonitor(
        tmp_path,
        buffer_size=16 * 1024,
        max_pending_hints=256,
    ).start()
    assert monitor.wait_until_ready(5)
    if monitor.state is not MonitorState.ACTIVE:
        batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
        pytest.skip(f"change monitor unavailable on fixture volume: {batch}")

    try:
        sequence = 0
        original = tmp_path / "before.txt"
        original.write_text("created", encoding="utf-8")
        sequence, _ = _wait_for_hint(
            monitor,
            after_sequence=sequence,
            action=ChangeAction.ADDED,
            name=original.name,
        )

        with original.open("a", encoding="utf-8") as stream:
            stream.write("\nmodified")
            stream.flush()
            os.fsync(stream.fileno())
        sequence, _ = _wait_for_hint(
            monitor,
            after_sequence=sequence,
            action=ChangeAction.MODIFIED,
            name=original.name,
        )

        renamed = tmp_path / "after.txt"
        original.rename(renamed)
        sequence, old_hints = _wait_for_hint(
            monitor,
            after_sequence=sequence,
            action=ChangeAction.RENAMED_OLD_NAME,
            name=original.name,
        )
        if not any(
            hint.action is ChangeAction.RENAMED_NEW_NAME
            and Path(hint.relative_path).name == renamed.name
            for hint in old_hints
        ):
            sequence, _ = _wait_for_hint(
                monitor,
                after_sequence=sequence,
                action=ChangeAction.RENAMED_NEW_NAME,
                name=renamed.name,
            )

        renamed.unlink()
        _wait_for_hint(
            monitor,
            after_sequence=sequence,
            action=ChangeAction.REMOVED,
            name=renamed.name,
        )
    finally:
        monitor.stop()

    assert monitor.state is MonitorState.STOPPED
