from __future__ import annotations

from pathlib import Path

from reclaimer.core.action_history import ActionEvent, ActionHistory


def _event(index: int) -> ActionEvent:
    return ActionEvent.create(
        action="AUTO_PERMANENT",
        category="USER_TEMP",
        path=rf"C:\Users\example\AppData\Local\Temp\fixture-{index}.tmp",
        logical_size=index,
        detail="handle verified",
    )


def test_history_is_redacted_and_returns_newest_events_first(tmp_path: Path) -> None:
    history = ActionHistory(tmp_path / "actions.jsonl")
    history.append(_event(1))
    history.append(_event(2))

    events = history.recent()

    assert [event.logical_size for event in events] == [2, 1]
    assert events[0].path.startswith("<EXTERNAL>")


def test_history_rotates_before_exceeding_its_bounded_payload(tmp_path: Path) -> None:
    history = ActionHistory(tmp_path / "actions.jsonl", max_bytes=400)
    history.append(_event(1))
    history.append(_event(2))

    assert history.path.is_file()
    assert history.path.with_suffix(".previous.jsonl").is_file()
    assert history.path.stat().st_size <= 400
