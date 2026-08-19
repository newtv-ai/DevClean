"""Windows per-drive Recycle Bin maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.windows_recycle_bin_maintenance import (
    RecycleBinCleanupResult,
    RecycleBinDriveInventory,
    empty_windows_recycle_bin,
    inventory_windows_recycle_bins,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventories: tuple[RecycleBinDriveInventory, ...]


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: RecycleBinCleanupResult | None
    error: str | None = None


class _WindowsRecycleBinMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Windows 回收站维护")
        self._window.geometry("820x560")
        self._window.minsize(700, 500)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._inventories: tuple[RecycleBinDriveInventory, ...] = ()
        self._busy = False
        self._status = tk.StringVar(value="尚未读取回收站。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="Windows 回收站维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "DevClean 使用 Windows Shell 自己的回收站 API，只查看并清空一个明确的本地固定磁盘。"
                "不会扫描或直接删除 $Recycle.Bin，也不会一次清空所有驱动器。"
            ),
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        warning = ttk.LabelFrame(root, text="USER_REVIEW", padding=8)
        warning.pack(fill=tk.X)
        ttk.Label(
            warning,
            text=(
                "回收站中的项目仍是可恢复的用户数据。清空后会永久失去这份恢复副本，"
                "因此不会默认选择，也不会因为占用空间大就自动执行。"
            ),
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        table_frame = ttk.Frame(root)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._tree = ttk.Treeview(
            table_frame,
            columns=("drive", "items", "size", "state"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        self._tree.heading("drive", text="驱动器")
        self._tree.heading("items", text="项目数")
        self._tree.heading("size", text="Shell 报告大小")
        self._tree.heading("state", text="状态")
        self._tree.column("drive", width=90, anchor=tk.W)
        self._tree.column("items", width=100, anchor=tk.E)
        self._tree.column("size", width=150, anchor=tk.E)
        self._tree.column("state", width=410, anchor=tk.W)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_buttons())

        ttk.Label(
            root,
            text=(
                "这里显示的是 Windows Shell 的逻辑回收站统计，不承诺等量增加物理空闲空间。"
                "执行前会再次读取统计；如果确认后内容发生变化，操作会中止并要求重新确认。"
            ),
            wraplength=780,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=780).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._empty_button = ttk.Button(
            footer,
            text="清空所选驱动器回收站…",
            command=self._confirm_empty,
            state=tk.DISABLED,
        )
        self._empty_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)
        self._start_inventory()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._refresh_buttons()

    def _selected_inventory(self) -> RecycleBinDriveInventory | None:
        selection = self._tree.selection()
        if len(selection) != 1:
            return None
        try:
            index = int(selection[0])
        except ValueError:
            return None
        if not 0 <= index < len(self._inventories):
            return None
        return self._inventories[index]

    def _refresh_buttons(self) -> None:
        selected = self._selected_inventory()
        enabled = (
            not self._busy
            and selected is not None
            and selected.cleanup_supported
            and selected.item_count > 0
        )
        self._empty_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过 Windows Shell 读取各固定磁盘回收站统计…")

        def work() -> None:
            try:
                inventories = inventory_windows_recycle_bins()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventories))

        threading.Thread(
            target=work,
            name="DevClean-Windows-recycle-bin-inventory",
            daemon=True,
        ).start()

    def _confirm_empty(self) -> None:
        if self._busy:
            return
        selected = self._selected_inventory()
        if selected is None or not selected.cleanup_supported or selected.item_count <= 0:
            return
        if not messagebox.askyesno(
            "确认永久清空回收站",
            (
                f"驱动器：{selected.root}\n"
                f"项目数：{selected.item_count:,}\n"
                f"Windows Shell 报告大小：{_format_bytes(selected.logical_bytes)}\n\n"
                "这会永久删除该驱动器当前用户回收站中的全部项目，之后不能从回收站恢复。\n\n"
                "DevClean 只会调用这个驱动器的 Windows Shell 回收站 API，不会清空其他驱动器。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新验证回收站统计并调用 Windows Shell 清空所选驱动器…")

        def work() -> None:
            try:
                result = empty_windows_recycle_bin(selected)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Windows-recycle-bin-empty",
            daemon=True,
        ).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(event, Exception):
            self._inventories = ()
            self._render()
            self._status.set(f"回收站读取失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventories = event.inventories
            self._render()
            self._status.set("回收站统计已刷新。")
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"没有报告清空成功：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._status.set(
                    "所选驱动器回收站已由 Windows Shell 报告为空；"
                    f"逻辑统计减少 {_format_bytes(result.reported_bytes_removed)}。"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for index, inventory in enumerate(self._inventories):
            state = "可由你确认清空" if inventory.cleanup_supported else inventory.reason
            self._tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    str(inventory.root),
                    f"{inventory.item_count:,}",
                    _format_bytes(inventory.logical_bytes),
                    state,
                ),
            )
        self._refresh_buttons()


def open_windows_recycle_bin_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WindowsRecycleBinMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_windows_recycle_bin_maintenance_dialog"]
