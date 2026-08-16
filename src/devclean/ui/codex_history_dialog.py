"""Explicit user-decision UI for Codex conversation and input history."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from devclean.core.codex_history import (
    CodexHistoryError,
    CodexSessionEntry,
    CodexThreadDeleteResult,
    codex_home,
    delete_codex_threads,
    prune_codex_input_history,
    scan_codex_sessions,
    select_codex_sessions_older_than,
    summarize_codex_input_history,
    summarize_codex_sessions,
)

_CUTOFFS = (30, 90, 180)


def open_codex_history_dialog(parent: tk.Misc) -> None:
    """Open history management without routing user history through AI."""

    home = codex_home()
    if home is None or not home.exists():
        messagebox.showinfo(
            "Codex 历史",
            "没有找到 CODEX_HOME / .codex 目录。",
            parent=parent,
        )
        return
    _CodexHistoryDialog(parent, home).show()


class _CodexHistoryDialog:
    def __init__(self, parent: tk.Misc, home: Path) -> None:
        self._parent = parent
        self._home = home
        self._window = tk.Toplevel(parent)
        self._window.title("Codex 历史管理")
        self._window.geometry("820x470")
        self._window.minsize(720, 420)
        self._cutoff = tk.StringVar(value="90")
        self._status = tk.StringVar(value="正在统计 Codex 历史…")
        self._worker_results: queue.Queue[CodexThreadDeleteResult | Exception] = queue.Queue()
        self._busy = False
        self._sessions: tuple[CodexSessionEntry, ...] = ()

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Codex 历史由你决定",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "会话不是缓存：DevClean 只按最近使用时间统计，删除时调用 Codex 自己的 "
                "thread/delete；输入历史按 history.jsonl 每条记录的 ts 裁剪。默认不删除。"
            ),
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._tree = ttk.Treeview(
            container,
            columns=("cutoff", "sessions", "session_size", "inputs", "input_size"),
            show="headings",
            height=5,
        )
        headings = {
            "cutoff": "范围",
            "sessions": "会话数",
            "session_size": "会话空间",
            "inputs": "输入记录数",
            "input_size": "输入历史空间",
        }
        widths = {
            "cutoff": 150,
            "sessions": 100,
            "session_size": 140,
            "inputs": 120,
            "input_size": 150,
        }
        for column, heading in headings.items():
            self._tree.heading(column, text=heading)
            self._tree.column(column, width=widths[column], anchor=tk.CENTER)
        self._tree.pack(fill=tk.X, pady=(0, 12))

        controls = ttk.Frame(container)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="用户选择阈值：").pack(side=tk.LEFT)
        self._cutoff_box = ttk.Combobox(
            controls,
            textvariable=self._cutoff,
            values=tuple(str(value) for value in _CUTOFFS),
            state="readonly",
            width=7,
        )
        self._cutoff_box.pack(side=tk.LEFT)
        ttk.Label(controls, text=" 天以上").pack(side=tk.LEFT, padx=(2, 14))
        self._delete_sessions_button = ttk.Button(
            controls,
            text="删除这些 Codex 会话…",
            command=self._delete_sessions,
        )
        self._delete_sessions_button.pack(side=tk.LEFT, padx=(0, 8))
        self._prune_inputs_button = ttk.Button(
            controls,
            text="清理这些输入历史…",
            command=self._prune_inputs,
        )
        self._prune_inputs_button.pack(side=tk.LEFT)

        ttk.Separator(container).pack(fill=tk.X, pady=14)
        ttk.Label(
            container,
            text=(
                "注意：会话删除是永久操作，不经过 Windows 回收站。Codex 会执行自己的引用/"
                "派生线程一致性检查；若 Codex 拒绝某个线程，DevClean 不会退回到直接删文件。"
            ),
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(12, 0))

        footer = ttk.Frame(container)
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(16, 0))
        ttk.Button(footer, text="刷新", command=self._refresh).pack(
            side=tk.RIGHT,
            padx=(8, 0),
        )
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)
        self._refresh()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _refresh(self) -> None:
        if self._busy:
            return
        try:
            sessions = scan_codex_sessions(self._home)
            session_summary = summarize_codex_sessions(sessions)
            input_summary = summarize_codex_input_history(self._home / "history.jsonl")
        except CodexHistoryError as error:
            messagebox.showerror("Codex 历史", str(error), parent=self._window)
            return
        self._sessions = sessions
        for item in self._tree.get_children():
            self._tree.delete(item)
        input_by_cutoff = {item.cutoff_days: item for item in input_summary}
        for summary in session_summary:
            input_item = input_by_cutoff[summary.cutoff_days]
            self._tree.insert(
                "",
                tk.END,
                values=(
                    f">= {summary.cutoff_days} 天",
                    f"{summary.count:,}",
                    _format_bytes(summary.logical_bytes),
                    f"{input_item.removable_records:,}",
                    _format_bytes(input_item.removable_bytes),
                ),
            )
        self._status.set(
            f"共找到 {len(sessions):,} 个 Codex 会话。表中是累计的“超过 N 天”删除收益；"
            "30/90/180 天只是用户选择阈值，不是安全阈值。"
        )
        self._sync_buttons()

    def _delete_sessions(self) -> None:
        if self._busy:
            return
        cutoff = int(self._cutoff.get())
        selected = select_codex_sessions_older_than(self._sessions, cutoff)
        if not selected:
            messagebox.showinfo(
                "Codex 历史",
                f"没有 {cutoff} 天以上的 Codex 会话。",
                parent=self._window,
            )
            return
        total_bytes = sum(item.logical_size for item in selected)
        if not messagebox.askyesno(
            "永久删除 Codex 会话",
            (
                f"将请求 Codex 永久删除 {len(selected):,} 个 {cutoff} 天以上的会话，"
                f"当前 rollout 文件约 {_format_bytes(total_bytes)}。\n\n"
                "这不是缓存清理，不经过回收站。Codex 可能按自己的线程关系一并删除派生子线程；"
                "存在外部引用时也可能拒绝删除。\n\n确认继续？"
            ),
            parent=self._window,
        ):
            return
        self._set_busy(True, "正在通过 Codex thread/delete 删除用户选中的会话…")

        def work() -> None:
            try:
                result = delete_codex_threads(selected, home=self._home)
            except Exception as error:  # noqa: BLE001 - surface worker failure in the UI
                self._worker_results.put(error)
            else:
                self._worker_results.put(result)

        threading.Thread(
            target=work,
            name="DevClean-Codex-history-delete",
            daemon=True,
        ).start()
        self._window.after(100, self._poll_delete_result)

    def _poll_delete_result(self) -> None:
        try:
            outcome = self._worker_results.get_nowait()
        except queue.Empty:
            if self._window.winfo_exists():
                self._window.after(100, self._poll_delete_result)
            return
        self._set_busy(False, "Codex 会话删除已完成。")
        if isinstance(outcome, Exception):
            messagebox.showerror("Codex 会话删除失败", str(outcome), parent=self._window)
            self._refresh()
            return
        if outcome.failed:
            sample = "\n".join(
                f"{thread_id}: {reason}" for thread_id, reason in outcome.failed[:5]
            )
            messagebox.showwarning(
                "部分会话未删除",
                (
                    f"请求 {outcome.requested:,} 个，已删除或已随父线程删除 "
                    f"{outcome.deleted_or_already_absent:,} 个，失败 {len(outcome.failed):,} 个。\n\n"
                    f"{sample}"
                ),
                parent=self._window,
            )
        else:
            messagebox.showinfo(
                "Codex 历史",
                f"已由 Codex 处理 {outcome.deleted_or_already_absent:,} 个会话。",
                parent=self._window,
            )
        self._refresh()

    def _prune_inputs(self) -> None:
        if self._busy:
            return
        cutoff = int(self._cutoff.get())
        summaries = summarize_codex_input_history(self._home / "history.jsonl")
        selected = next(item for item in summaries if item.cutoff_days == cutoff)
        if selected.removable_records == 0:
            messagebox.showinfo(
                "Codex 输入历史",
                f"没有 {cutoff} 天以上的输入历史记录。",
                parent=self._window,
            )
            return
        if not messagebox.askyesno(
            "清理 Codex 输入历史",
            (
                f"将从 history.jsonl 中删除 {selected.removable_records:,} 条 {cutoff} 天以上的"
                f"输入记录，预计释放 {_format_bytes(selected.removable_bytes)}。\n\n"
                "较新的记录和无法识别时间的记录会保留。确认继续？"
            ),
            parent=self._window,
        ):
            return
        try:
            result = prune_codex_input_history(self._home / "history.jsonl", cutoff)
        except CodexHistoryError as error:
            messagebox.showerror("Codex 输入历史", str(error), parent=self._window)
            return
        messagebox.showinfo(
            "Codex 输入历史",
            (
                f"已删除 {result.removed_records:,} 条记录，释放约 "
                f"{_format_bytes(result.removed_bytes)}；保留 {result.kept_records:,} 条。"
            ),
            parent=self._window,
        )
        self._refresh()

    def _set_busy(self, busy: bool, status: str) -> None:
        self._busy = busy
        self._status.set(status)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        state = tk.DISABLED if self._busy else tk.NORMAL
        self._delete_sessions_button.configure(state=state)
        self._prune_inputs_button.configure(state=state)
        self._cutoff_box.configure(state="disabled" if self._busy else "readonly")


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_codex_history_dialog"]
