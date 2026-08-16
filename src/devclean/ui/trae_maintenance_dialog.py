"""Read-only Trae hidden-storage inventory."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from devclean.core.trae_maintenance import TraeStorageInventory, inventory_trae_storage


class _TraeMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Trae 存储维护")
        self._window.geometry("900x600")
        self._window.minsize(780, 500)
        self._events: queue.Queue[TraeStorageInventory | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Trae 隐藏存储…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Trae 隐藏存储",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Cache / CachedData / Code Cache / GPUCache / DawnCache / 扩展下载缓存 / logs / "
                "Crashpad 由主扫描按 TOOL 规则处理。这里展示 Trae 未公开充分删除语义的工作区、"
                "AI/扩展状态、本地历史、恢复数据和 .trae 持久目录。"
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
        self._tree.column("kind", width=300, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("path", width=480, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        note = ttk.LabelFrame(container, text="为什么这些不直接删", padding=10)
        note.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            note,
            text=(
                "Trae 的 AI 会话/索引/项目状态并没有像 Cursor 那样公开稳定的 GC/历史删除接口。"
                "因此 DevClean 先展示它们的空间收益，但不会凭 VS Code 的目录结构猜测它们可重建。"
                "如果后续 Trae 提供明确的历史/索引清理命令，再接成专用动作。"
            ),
            wraplength=830,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

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
        self._status.set("正在统计 Trae 隐藏存储…")

        def work() -> None:
            try:
                result = inventory_trae_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(result)

        threading.Thread(target=work, name="DevClean-Trae-storage", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if isinstance(outcome, Exception):
            self._status.set(f"Trae 存储统计失败：{outcome}")
        else:
            self._render(outcome)
        self._window.after(100, self._poll)

    def _render(self, inventory: TraeStorageInventory) -> None:
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
        self._status.set(
            f"已定位 {len(visible)} 处 Trae 持久存储，总计约 "
            f"{_format_bytes(sum(entry.logical_bytes for entry in visible))}。"
        )


def open_trae_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _TraeMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_trae_maintenance_dialog"]
