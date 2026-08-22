"""Product shell additions that do not change DevClean classification policy.

The product contract is rule-first: audited rules decide known items, unresolved
items stay in the review/AI lane, and the UI must not invent new cleanup policy.
This wrapper only adds visible scan timing to the modern window.
"""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk

from devclean.core.triage import TriageSession
from devclean.ui.modern_app import ModernDevCleanWindow


class ProductDevCleanWindow(ModernDevCleanWindow):
    """Modern DevClean window with click-to-result scan timing."""

    def __init__(self, root: tk.Tk) -> None:
        self._scan_started_at: float | None = None
        self._scan_duration = tk.StringVar(master=root, value="扫描耗时：—")
        super().__init__(root)

    def _build_status(self, page: ttk.Frame) -> None:
        super()._build_status(page)
        ttk.Label(
            page,
            textvariable=self._scan_duration,
            style="BodyOnPage.TLabel",
        ).pack(anchor=tk.E, pady=(0, 8))

    def _start_scan(self) -> None:
        self._scan_started_at = time.monotonic()
        self._scan_duration.set("扫描耗时：计时中…")
        super()._start_scan()
        if self._busy != "scanning":
            self._scan_started_at = None
            self._scan_duration.set("扫描耗时：—")

    def _publish(self, session: TriageSession) -> None:
        super()._publish(session)
        started = self._scan_started_at
        if started is None:
            return
        elapsed = max(0.0, time.monotonic() - started)
        self._scan_started_at = None
        if elapsed < 60:
            rendered = f"{elapsed:.1f} 秒"
        else:
            minutes, seconds = divmod(elapsed, 60)
            rendered = f"{int(minutes)} 分 {seconds:.0f} 秒"
        self._scan_duration.set(f"扫描耗时：{rendered}")


__all__ = ["ProductDevCleanWindow"]
