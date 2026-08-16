"""Read-only VS Code hidden-storage inventory and chat cleanup guidance."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from devclean.core.vscode_maintenance import (
    VSCodeStorageInventory,
    inventory_vscode_storage,
)

_CLEAR_WORKSPACE_CHATS = "Chat: Clear All Workspace Chats"


class _VSCodeMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("VS Code 存储维护")
        self._window.geometry("900x620")
        self._window.minsize(780, 520)
        self._events: queue.Queue[VSCodeStorageInventory | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 VS Code 隐藏存储…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="VS Code 隐藏存储",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Cache / CachedData / Code Cache / GPUCache / DawnCache / 扩展 VSIX 缓存 / logs "
                "由主扫描按 TOOL 规则处理。这里展示不能被伪装成缓存删除的工作区、聊天、"
                "本地历史、未保存文件恢复和扩展状态。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._tree = ttk.Treeview(
            container,
            columns=("kind", "size", "path"),
            show="headings",
            height=12,
        )
        self._tree.heading("kind", text="内容")
        self._tree.heading("size", text="占用")
        self._tree.heading("path", text="位置")
        self._tree.column("kind", width=280, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("path", width=500, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        chat_box = ttk.LabelFrame(container, text="VS Code 聊天清理", padding=10)
        chat_box.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(
            chat_box,
            text=(
                "不要直接删除 workspaceStorage/state.vscdb。打开 VS Code，按 Ctrl+Shift+P，"
                f"运行 `{_CLEAR_WORKSPACE_CHATS}` 清当前工作区聊天；其他历史会话在 Sessions "
                "列表中逐条 Delete。聊天删除是永久操作。"
            ),
            wraplength=720,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            chat_box,
            text="复制清理命令",
            command=lambda: self._copy(_CLEAR_WORKSPACE_CHATS),
        ).pack(side=tk.RIGHT, padx=(12, 0))

        ttk.Label(
            container,
            text=(
                "Backups 是未保存编辑器/Hot Exit 恢复数据，User/globalStorage 是扩展和全局状态；"
                "这两类即使很大也不会进入普通垃圾删除器。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=860,
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
        self._status.set("正在统计 VS Code 隐藏存储…")

        def work() -> None:
            try:
                result = inventory_vscode_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(result)

        threading.Thread(target=work, name="DevClean-VSCode-storage", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if isinstance(outcome, Exception):
            self._status.set(f"VS Code 存储统计失败：{outcome}")
        else:
            self._render(outcome)
        self._window.after(100, self._poll)

    def _render(self, inventory: VSCodeStorageInventory) -> None:
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
            f"已定位 {len(visible)} 处 VS Code 持久存储，总计约 "
            f"{_format_bytes(sum(entry.logical_bytes for entry in visible))}。"
        )

    def _copy(self, text: str) -> None:
        self._window.clipboard_clear()
        self._window.clipboard_append(text)
        self._status.set(f"已复制：{text}")


def open_vscode_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _VSCodeMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_vscode_maintenance_dialog"]
