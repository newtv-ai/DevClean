from __future__ import annotations

import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import TclError

import pytest

from reclaimer.core.triage import ReviewLane, TriageSession, triage_file
from reclaimer.scanner import ScanRecord, ScanRecordKind
from reclaimer.ui.app import ReclaimerWindow
from reclaimer.ui.app import main as gui_main


def _record(path: Path, *, last_write_time_ns: int | None) -> ScanRecord:
    return ScanRecord(
        root=str(path.parent),
        path=str(path),
        kind=ScanRecordKind.FILE,
        depth=1,
        logical_size=100,
        allocated_size=4096,
        raw_allocated_size=4096,
        last_write_time_ns=last_write_time_ns,
    )


def test_old_user_temp_file_enters_auto_clean_lane(tmp_path: Path) -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    item = triage_file(
        _record(tmp_path / "old.tmp", last_write_time_ns=0),
        now=now,
        temp_root=tmp_path,
    )

    assert item.lane is ReviewLane.AUTO_CLEAN


def test_known_cache_requires_ai_explanation(tmp_path: Path) -> None:
    item = triage_file(
        _record(tmp_path / "huggingface" / "model.bin", last_write_time_ns=None),
        temp_root=tmp_path / "temp",
    )

    assert item.lane is ReviewLane.AI_REVIEW


def test_protected_asset_is_never_an_ai_or_auto_candidate(tmp_path: Path) -> None:
    item = triage_file(
        _record(tmp_path / ".git" / "config", last_write_time_ns=0),
        temp_root=tmp_path,
    )

    assert item.lane is ReviewLane.PROTECTED


def test_session_keeps_bounded_display_items_but_exact_totals() -> None:
    session = TriageSession(display_limit=2)
    for size in (1, 3, 2):
        session.add(
            triage_file(
                ScanRecord(
                    root=r"C:\Temp",
                    path=rf"C:\Temp\{size}.tmp",
                    kind=ScanRecordKind.FILE,
                    depth=1,
                    logical_size=size,
                    allocated_size=size,
                    raw_allocated_size=size,
                    last_write_time_ns=0,
                ),
                now=datetime(2026, 7, 12, tzinfo=UTC),
                temp_root=Path(r"C:\Temp"),
            )
        )

    assert session.summary(ReviewLane.AUTO_CLEAN).files == 3
    assert session.summary(ReviewLane.AUTO_CLEAN).logical_bytes == 6
    assert [item.logical_size for item in session.items(ReviewLane.AUTO_CLEAN)] == [3, 2]


def test_gui_smoke_does_not_create_a_window_or_state_database() -> None:
    assert gui_main(["--smoke"]) == 0


def test_gui_constructs_native_widgets_without_a_state_database() -> None:
    try:
        root = tk.Tk()
    except TclError as error:
        pytest.skip(f"Tk desktop is unavailable: {error}")
    root.withdraw()
    try:
        window = ReclaimerWindow(root)
        assert window._trees
    finally:
        root.destroy()
