"""Read-only Cursor storage inventory and vendor-supported cleanup guidance."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from devclean.core.cursor_maintenance import (
    CursorStorageInventory,
    inventory_cursor_storage,
)

_DELETE_OLD_CHATS = "Developer: Delete Old Chats..."
_GC_AGENT_BLOBS = "Developer: GC Agent KV Blobs"


class _CursorMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Cursor 存储维护")
        self._window.geometry("880x600")
        self._window.minsize(760, 500)
        self._events: queue.Queue[CursorStorageInventory | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Cursor 隐藏存储…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Cursor 隐藏存储与聊天数据库",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Cache / CachedData / Code Cache / GPUCache / 扩展下载缓存和 logs 由主扫描"
                "按 TOOL 规则处理。这里展示不能当普通缓存删掉的聊天、workspace 状态、"
                "Agent transcripts 和检查点。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._tree = ttk.Treeview(
            container,
            columns=("kind", "size", "path"),
            show="headings",
            height=10,
        )
        self._tree.heading("kind", text="内容")
        self._tree.heading("size", text="占用")
        self._tree.heading("path", text="位置")
        self._tree.column("kind", width=240, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("path", width=490, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        command_box = ttk.LabelFrame(container, text="Cursor 官方历史清理", padding=10)
        command_box.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            command_box,
            text=(
                "不要删除 state.vscdb。打开 Cursor，按 Ctrl+Shift+P，依次运行：\n"
                f"1. {_DELETE_OLD_CHATS}  → 输入要保留的天数\n"
                f"2. {_GC_AGENT_BLOBS}  → 清孤立 Agent blob 并压缩数据库"
            ),
            wraplength=800,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        button_frame = ttk.Frame(command_box)
        button_frame.pack(side=tk.RIGHT, padx=(12, 0))
        ttk.Button(
            button_frame,
            text="复制第 1 条命令",
            command=lambda: self._copy(_DELETE_OLD_CHATS),
        ).pack(fill=tk.X)
        ttk.Button(
            button_frame,
            text="复制第 2 条命令",
            command=lambda: self._copy(_GC_AGENT_BLOBS),
        ).pack(fill=tk.X, pady=(6, 0))

        ttk.Label(
            container,
            text=(
                "workspaceStorage、User/History、checkpoints、.cursor/projects 和 .cursor/chats "
                "属于 USER 数据。即使很大，DevClean 也不会把它们伪装成缓存自动删除。"
            ),
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=840,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

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
        self._status.set("正在统计 Cursor 隐藏存储…")

        def work() -> None:
            try:
                result = inventory_cursor_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(result)

        threading.Thread(target=work, name="DevClean-Cursor-storage", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if isinstance(outcome, Exception):
            self._status.set(f"Cursor 存储统计失败：{outcome}")
        else:
            self._render(outcome)
        self._window.after(100, self._poll)

    def _render(self, inventory: CursorStorageInventory) -> None:
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
            f"已定位 {len(visible)} 处 Cursor 持久存储，总计约 "
            f"{_format_bytes(sum(entry.logical_bytes for entry in visible))}。"
        )

    def _copy(self, text: str) -> None:
        self._window.clipboard_clear()
        self._window.clipboard_append(text)
        self._status.set(f"已复制：{text}")


def open_cursor_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _CursorMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_cursor_maintenance_dialog"]
