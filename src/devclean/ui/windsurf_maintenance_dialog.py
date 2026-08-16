"""Read-only Windsurf hidden-storage inventory."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from devclean.core.windsurf_maintenance import (
    WindsurfStorageInventory,
    inventory_windsurf_storage,
)


class _WindsurfMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Windsurf 存储维护")
        self._window.geometry("900x600")
        self._window.minsize(780, 500)
        self._events: queue.Queue[WindsurfStorageInventory | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Windsurf 隐藏存储…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Windsurf / Codeium 隐藏存储",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "标准 Electron cache / Code Cache / GPUCache / 扩展下载缓存 / logs / Crashpad "
                "由主扫描处理。这里专门展示 workspace/global AI 状态、History、Backups、"
                ".windsurf 和共享 .codeium；这些不会因为占空间大就整树删除。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._tree = ttk.Treeview(
            container,
            columns=("kind", "size", "path"),
            show="headings",
            height=13,
        )
        self._tree.heading("kind", text="内容")
        self._tree.heading("size", text="占用")
        self._tree.heading("path", text="位置")
        self._tree.column("kind", width=310, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("path", width=470, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        note = ttk.LabelFrame(container, text="AI / 索引状态", padding=10)
        note.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            note,
            text=(
                "Windsurf/Codeium 的索引和 AI 状态可能跨 User/globalStorage、.windsurf 和 .codeium。"
                "在没有稳定的厂商 GC/重建证据前，DevClean 只显示大小，不直接删这些根。"
            ),
            wraplength=830,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(container, textvariable=self._status, wraplength=860).pack(
            anchor=tk.W,
            pady=(8, 0),
        )
        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="重新统计", command=self._start_inventory).pack(
            side=tk.RIGHT,
            padx=(8, 0),
        )
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)
        self._window.after(100, self._poll)
        self._start_inventory()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _start_inventory(self) -> None:
        self._status.set("正在统计 Windsurf 隐藏存储…")

        def work() -> None:
            try:
                self._events.put(inventory_windsurf_storage())
            except Exception as error:
                self._events.put(error)

        threading.Thread(target=work, name="DevClean-Windsurf-storage", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if isinstance(outcome, Exception):
            self._status.set(f"Windsurf 存储统计失败：{outcome}")
        else:
            self._render(outcome)
        self._window.after(100, self._poll)

    def _render(self, inventory: WindsurfStorageInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        visible = [entry for entry in inventory.entries if entry.exists]
        for entry in sorted(visible, key=lambda item: item.logical_bytes, reverse=True):
            kind = f"USER · {entry.label}" if entry.user_data else f"KEEP · {entry.label}"
            self._tree.insert(
                "",
                tk.END,
                values=(kind, _format_bytes(entry.logical_bytes), str(entry.path)),
            )
        total = sum(entry.logical_bytes for entry in visible)
        self._status.set(
            f"已定位 {len(visible)} 处 Windsurf/Codeium 持久存储，总计约 {_format_bytes(total)}。"
        )


def open_windsurf_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WindsurfMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_windsurf_maintenance_dialog"]
