"""The window stays responsive during a scan and reports the finished scan.

No Tk window is created: trees, labels and the event loop are small fakes.
"""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import time
from queue import Queue
from typing import Any, cast

import pytest

from devclean.ui import app, product_app


class _Tree:
    """Records the Tk calls a table update makes."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.calls: list[tuple[str, object]] = []

    def __str__(self) -> str:
        return ".results"

    def insert(self, _parent: str, index: int, *, iid: str, values: object) -> None:
        self.calls.append(("insert", iid))
        self.rows.insert(index, iid)

    def delete(self, *iids: str) -> None:
        self.calls.append(("delete", iids))
        for iid in iids:
            self.rows.remove(iid)

    def move(self, iid: str, _parent: str, index: int) -> None:
        self.calls.append(("move", iid))
        self.rows.remove(iid)
        self.rows.insert(index, iid)

    def item(self, iid: str, *, values: object) -> None:
        self.calls.append(("item", iid))


class _Var:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


def _window() -> Any:
    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._deletable_tree = _Tree()
    window._checked = set()
    return window


def test_a_scan_preview_touches_only_the_rows_that_changed() -> None:
    window = _window()
    tree = window._deletable_tree
    rows = [(rf"C:\cache\{index}.bin", 1000 - index, False) for index in range(5)]

    window._update_live_rows(tree, rows)
    assert tree.rows == [row[0] for row in rows]

    tree.calls.clear()
    window._update_live_rows(tree, rows)
    assert tree.calls == []  # an unchanged preview costs nothing

    arrived = (r"C:\cache\new.bin", 997, False)
    changed = [*rows[:2], arrived, *rows[2:4]]
    window._update_live_rows(tree, changed)
    assert tree.calls == [("delete", (rows[4][0],)), ("insert", arrived[0])]
    assert tree.rows == [row[0] for row in changed]

    tree.calls.clear()
    grown = [(changed[0][0], 5000, False), *changed[1:]]
    window._update_live_rows(tree, grown)
    assert tree.calls == [("item", changed[0][0])]

    tree.calls.clear()
    reordered = [grown[1], grown[0], *grown[2:]]
    window._update_live_rows(tree, reordered)
    assert len(tree.calls) == 1 and tree.calls[0][0] == "move"
    assert tree.rows == [row[0] for row in reordered]


def test_a_finished_scan_publishes_results_before_reporting_its_totals() -> None:
    scheduled: list[object] = []
    order: list[tuple[str, object]] = []

    class Root:
        def after(self, _delay: int, callback: object) -> None:
            scheduled.append(callback)

    class Progress:
        def stop(self) -> None:
            order.append(("progress", "stopped"))

    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._root = Root()
    window._events = Queue()
    window._pending_event = None
    window._scan_token = "scan"
    window._progress = Progress()
    window._publish = lambda session, precomputed=None: order.append(
        ("publish", (session, precomputed))
    )
    window._scan_finished = lambda cancelled, totals: order.append(
        ("finished", (cancelled, totals))
    )
    totals = app.ScanTotals(files=10, directories=3, logical_bytes=1234)
    partition = object()
    window._events.put(("scan_done", ("scan", "session", False, totals, partition)))
    window._events.put(("scan_done", ("scan", "older", True)))  # a worker without totals

    window._drain_events()

    assert order == [
        ("progress", "stopped"),
        ("publish", ("session", partition)),
        ("finished", (False, totals)),
        ("progress", "stopped"),
        ("publish", ("older", None)),
        ("finished", (True, None)),
    ]
    assert scheduled == [window._drain_events]


class _Root:
    """A Tk root whose ``after`` queue is run by hand."""

    def __init__(self) -> None:
        self.pending: dict[str, tuple[object, tuple[object, ...]]] = {}
        self.cancelled: list[str] = []
        self._next = 0

    def after(self, _delay: int, callback: object, *args: object) -> str:
        self._next += 1
        job = f"after#{self._next}"
        self.pending[job] = (callback, args)
        return job

    def after_cancel(self, job: str) -> None:
        self.cancelled.append(job)
        self.pending.pop(job, None)

    def run_pending(self) -> None:
        due, self.pending = self.pending, {}
        for callback, args in due.values():
            cast(Any, callback)(*args)


def _product_window() -> Any:
    window: Any = product_app.ProductDevCleanWindow.__new__(product_app.ProductDevCleanWindow)
    window._root = _Root()
    window._scan_duration = _Var("扫描耗时：—")
    window._status = _Var()
    window._session = None
    window._scan_token = "scan"
    window._scan_clock_job = None
    return window


@pytest.mark.parametrize(
    ("elapsed", "text"),
    [
        (0, "扫描耗时：0 秒"),
        (45.9, "扫描耗时：45 秒"),
        (65, "扫描耗时：1 分 05 秒"),
        (150, "扫描耗时：2 分 30 秒"),
        (3725, "扫描耗时：62 分 05 秒"),
    ],
)
def test_the_live_clock_renders_whole_elapsed_seconds(elapsed: float, text: str) -> None:
    assert product_app._live_elapsed_text(elapsed) == text


def test_the_timer_shows_the_real_elapsed_time_while_scanning_and_the_total_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A user requirement (2026-11-11): the label counts the real scan time live.
    window = _product_window()
    root = window._root
    window._scan_started_at = time.monotonic() - 75
    window._schedule_scan_clock("scan")
    assert len(root.pending) == 1
    root.run_pending()
    assert window._scan_duration.get() == "扫描耗时：1 分 15 秒"
    assert len(root.pending) == 1  # the next tick, about a second later

    # A new schedule (a rescan) replaces the pending tick instead of adding one.
    window._schedule_scan_clock("scan")
    assert len(root.pending) == 1 and len(root.cancelled) == 1

    # Republishing results (an AI import, a rule edit) does not stop the clock.
    monkeypatch.setattr(app.DevCleanWindow, "_publish", lambda *_args, **_kwargs: None)
    product_app.ProductDevCleanWindow._publish(window, cast(Any, object()))
    assert window._scan_started_at is not None and len(root.pending) == 1

    window._scan_finished(False, app.ScanTotals(405_827, 51_234, 118 * 1024**3))

    assert window._scan_started_at is None
    assert root.pending == {}  # the clock stopped with the scan
    assert window._scan_duration.get().startswith("扫描总耗时：1 分 1")
    status = window._status.get()
    assert status.startswith("扫描完成：共 405,827 个文件、51,234 个文件夹，总大小 ")
    assert "，总耗时 1 分 1" in status


def test_a_stale_tick_neither_overwrites_the_total_nor_reschedules() -> None:
    window = _product_window()
    root = window._root
    window._scan_started_at = time.monotonic() - 5
    window._tick_scan_clock("scan")
    assert window._scan_duration.get() == "扫描耗时：5 秒"
    assert len(root.pending) == 1

    # A tick of an earlier scan stops itself without touching the label.
    root.pending.clear()
    window._scan_duration.set("unchanged")
    window._tick_scan_clock("older-scan")
    assert window._scan_duration.get() == "unchanged" and root.pending == {}

    # After the scan is done, a tick that was already due changes nothing.
    window._scan_started_at = None
    window._tick_scan_clock("scan")
    assert window._scan_duration.get() == "unchanged" and root.pending == {}


def test_a_cancelled_scan_says_so_with_what_it_saw() -> None:
    window = _product_window()
    window._scan_started_at = time.monotonic() - 3
    window._scan_finished(True, app.ScanTotals(12, 4, 2048))
    assert window._scan_duration.get().startswith("扫描已取消，用时")
    assert window._status.get().startswith("扫描已取消；已看过共 12 个文件、4 个文件夹")


def test_rows_that_were_not_removed_stay_listed_with_their_reason() -> None:
    from types import SimpleNamespace

    from devclean.core.cleanup_journal import ActionState
    from devclean.core.postscan_cleanup import CleanupExecutionResult, CleanupRefusal
    from devclean.platform.windows.exact_cleanup import TempFolderPurgeResult

    gone, refused, partly = r"C:\a\gone", r"C:\a\refused", r"C:\a\partly"
    window: Any = app.DevCleanWindow.__new__(app.DevCleanWindow)
    window._deletable = [SimpleNamespace(path=path) for path in (gone, refused, partly)]
    window._unsure = []
    window._deletable_tree = window._unsure_tree = None
    window._checked = {gone, refused, partly}
    window._cleanup_failures = {}
    window._status = _Var()
    window._fill = lambda *_args: None
    window._refresh_totals = lambda: None
    window._sync_buttons = lambda: None
    left = TempFolderPurgeResult(
        root_path=partly,
        files_removed=2,
        links_removed=0,
        directories_removed=1,
        bytes_removed=2048,
        in_use=3,
        denied=0,
        kept=0,
        completed=True,
    )
    result = CleanupExecutionResult(
        action_states=(("a1", ActionState.PURGED), ("a2", ActionState.FAILED_UNCHANGED)),
        purged_logical_bytes=4096,
        completed_paths=(gone,),
        failures=((refused, CleanupRefusal(application_cleanup_refusal())),),
        partial=((partly, left),),
    )

    window._report_deletion((result,), ())

    assert [item.path for item in window._deletable] == [refused, partly]
    assert window._checked == {refused, partly}
    assert window._row_reason(refused, "原因") == "上次清理未完成：所属程序正在运行，请关闭后再清理"
    assert window._row_reason(partly, "原因").startswith(
        "上次清理未完成：已清理 2 个文件（2.0 KB），留下：正被程序使用 3 项"
    )
    assert window._row_reason(gone, "原因") == "原因"


def application_cleanup_refusal() -> str:
    from devclean.core.application_cleanup import RUNNING_OWNER_REFUSAL

    return RUNNING_OWNER_REFUSAL
