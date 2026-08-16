"""User-facing Claude Code vendor maintenance actions."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from devclean.core.claude_maintenance import (
    ClaudeMaintenanceError,
    ClaudePluginPruneResult,
    claude_plugin_storage_bytes,
    run_claude_plugin_prune,
)


class _ClaudeMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Claude Code 存储维护")
        self._window.geometry("780x520")
        self._window.minsize(680, 440)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._busy = False
        self._status = tk.StringVar(value="正在统计 Claude Code 插件存储…")
        self._plugin_total = tk.StringVar(value="—")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Claude Code 隐藏存储",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "普通缓存、debug、计划文件、任务状态、shell 快照、临时 task output 等由主扫描"
                "按应用规则处理。这里专门处理不能直接删目录的插件依赖。"
            ),
            wraplength=740,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        plugin_box = ttk.LabelFrame(container, text="Plugins", padding=10)
        plugin_box.pack(fill=tk.X)
        row = ttk.Frame(plugin_box)
        row.pack(fill=tk.X)
        ttk.Label(row, text="当前插件目录总占用：").pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self._plugin_total).pack(side=tk.LEFT)
        self._preview = ttk.Button(
            row,
            text="预览孤立依赖",
            command=lambda: self._start_prune(dry_run=True),
        )
        self._preview.pack(side=tk.RIGHT)
        self._prune = ttk.Button(
            row,
            text="执行 plugin prune…",
            command=self._confirm_prune,
            state=tk.DISABLED,
        )
        self._prune.pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Label(
            plugin_box,
            text=(
                "使用 Claude Code 自己的 `claude plugin prune --scope user`。它只移除已经没有"
                "任何已安装插件需要的自动安装依赖；DevClean 不会直接删除 ~/.claude/plugins。"
            ),
            wraplength=720,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        history_box = ttk.LabelFrame(container, text="用户历史（不属于缓存）", padding=10)
        history_box.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            history_box,
            text=(
                "projects 会话、file-history 检查点、history.jsonl 输入历史、stats-cache.json "
                "都被标记为 USER 数据：主扫描和 AI 都无权把它们变成普通垃圾。需要清项目状态时，"
                "应使用 Claude Code 自己的 project purge 语义，而不是直接删其中某个文件。"
            ),
            wraplength=720,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(container, text="Claude 输出：").pack(anchor=tk.W, pady=(12, 4))
        self._output = tk.Text(container, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self._output.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=740,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(50, self._poll)
        self._start_storage_count()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _start_storage_count(self) -> None:
        def work() -> None:
            try:
                value = claude_plugin_storage_bytes()
            except Exception as error:
                self._events.put(("storage_error", error))
            else:
                self._events.put(("storage", value))

        threading.Thread(target=work, name="DevClean-Claude-storage", daemon=True).start()

    def _start_prune(self, *, dry_run: bool) -> None:
        if self._busy:
            return
        self._busy = True
        self._sync_buttons()
        self._status.set(
            "正在让 Claude Code 计算可删除的孤立依赖…"
            if dry_run
            else "正在由 Claude Code 删除孤立插件依赖…"
        )

        def work() -> None:
            try:
                result = run_claude_plugin_prune(dry_run=dry_run)
            except Exception as error:
                self._events.put(("prune_error", error))
            else:
                self._events.put(("prune", result))

        threading.Thread(target=work, name="DevClean-Claude-prune", daemon=True).start()

    def _confirm_prune(self) -> None:
        if not messagebox.askyesno(
            "Claude plugin prune",
            (
                "将调用 Claude Code 官方 plugin prune 删除无人依赖的自动安装插件依赖。\n\n"
                "你直接安装的插件不属于 prune 对象；DevClean 也不会直接删 plugins 目录。\n\n"
                "请先关闭所有 Claude Code 窗口。确认继续？"
            ),
            parent=self._window,
        ):
            return
        self._start_prune(dry_run=False)

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self._window.after(100, self._poll)

    def _handle_event(self, kind: str, payload: object) -> None:
        if kind == "storage":
            if not isinstance(payload, int):
                self._status.set("插件目录统计返回了无效结果。")
                return
            self._plugin_total.set(_format_bytes(payload))
            self._status.set("可先预览 Claude Code 判定的孤立插件依赖。")
            return
        if kind == "storage_error":
            self._status.set(f"插件目录统计失败：{payload}")
            return
        if kind == "prune_error":
            self._busy = False
            self._sync_buttons()
            error = (
                payload
                if isinstance(payload, Exception)
                else ClaudeMaintenanceError(str(payload))
            )
            messagebox.showerror("Claude plugin prune", str(error), parent=self._window)
            self._status.set("Claude plugin prune 未执行。")
            return
        if kind != "prune" or not isinstance(payload, ClaudePluginPruneResult):
            return
        self._busy = False
        self._write_output(payload.output or "Claude Code 没有返回文本输出。")
        if payload.dry_run:
            self._prune.configure(state=tk.NORMAL)
            self._status.set("预览完成。若需要释放空间，可执行 plugin prune。")
        else:
            self._status.set("Claude plugin prune 已完成；正在重新统计插件目录。")
            self._start_storage_count()
        self._sync_buttons()

    def _write_output(self, text: str) -> None:
        self._output.configure(state=tk.NORMAL)
        self._output.delete("1.0", tk.END)
        self._output.insert("1.0", text)
        self._output.configure(state=tk.DISABLED)

    def _sync_buttons(self) -> None:
        self._preview.configure(state=tk.DISABLED if self._busy else tk.NORMAL)
        if self._busy:
            self._prune.configure(state=tk.DISABLED)


def open_claude_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _ClaudeMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_claude_maintenance_dialog"]
