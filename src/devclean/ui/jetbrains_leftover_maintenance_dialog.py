"""JetBrains vendor-expired system-directory maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk

from devclean.core.jetbrains_leftover_maintenance import (
    JetBrainsLeftoverCleanupResult,
    JetBrainsLeftoverInventory,
    cleanup_jetbrains_expired_system_directory,
    inventory_jetbrains_expired_system_directories,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventories: tuple[JetBrainsLeftoverInventory, ...]


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: JetBrainsLeftoverCleanupResult | None
    error: str | None = None


class _JetBrainsLeftoverMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("JetBrains 过期版本存储维护")
        self._window.geometry("960x620")
        self._window.minsize(820, 540)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._inventories: tuple[JetBrainsLeftoverInventory, ...] = ()
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 JetBrains 过期版本存储。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="JetBrains 过期版本存储维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "JetBrains 当前文档和源码定义了 180 天旧版本自动清理生命周期。"
                "DevClean 只镜像这个更窄的厂商边界：默认 system 目录整棵树至少 180 天未更新，"
                "并且没有对应的现存 IDE 安装。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        note = ttk.LabelFrame(root, text="不会碰什么", padding=8)
        note.pack(fill=tk.X)
        ttk.Label(
            note,
            text=(
                "不会删除 %APPDATA% 下的 IDE 配置、用户插件，也不会处理自定义 idea.system.path。"
                "注意：旧 system 目录本身可能包含 Local History；JetBrains 的自动过期清理同样删除"
                "这棵旧 system 树。因此这里只对已经超过厂商 180 天期限且确认没有现存安装的版本开放。"
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        table_frame = ttk.Frame(root)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._tree = ttk.Treeview(
            table_frame,
            columns=("version", "age", "size", "installed", "state"),
            show="headings",
            selectmode="browse",
            height=14,
        )
        for column, title in (
            ("version", "产品 / 版本"),
            ("age", "最近更新"),
            ("size", "逻辑大小"),
            ("installed", "安装状态"),
            ("state", "DevClean 判定"),
        ):
            self._tree.heading(column, text=title)
        self._tree.column("version", width=190, anchor=tk.W)
        self._tree.column("age", width=110, anchor=tk.E)
        self._tree.column("size", width=120, anchor=tk.E)
        self._tree.column("installed", width=110, anchor=tk.W)
        self._tree.column("state", width=400, anchor=tk.W)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_buttons())

        ttk.Label(
            root,
            text=(
                "删除前会重新扫描整棵 system 树、重新判断 .home/product-info 安装状态、刷新 JetBrains "
                "进程状态，并核对稳定目录身份。任何变化都会中止。显示大小是逻辑统计，不等于物理回收空间。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=920).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._cleanup_button = ttk.Button(
            footer,
            text="清理所选厂商过期 system 目录…",
            command=self._confirm_cleanup,
            state=tk.DISABLED,
        )
        self._cleanup_button.pack(side=tk.RIGHT, padx=(8, 0))
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

    def _selected(self) -> JetBrainsLeftoverInventory | None:
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
        selected = self._selected()
        enabled = not self._busy and selected is not None and selected.cleanup_supported
        self._cleanup_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在读取 JetBrains 默认版本目录并计算整棵 system 树的最后更新时间…")

        def work() -> None:
            try:
                inventories = inventory_jetbrains_expired_system_directories()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventories))

        threading.Thread(
            target=work,
            name="DevClean-JetBrains-leftover-inventory",
            daemon=True,
        ).start()

    def _confirm_cleanup(self) -> None:
        if self._busy:
            return
        selected = self._selected()
        if selected is None or not selected.cleanup_supported:
            return
        if not messagebox.askyesno(
            "确认删除 JetBrains 厂商过期 system 目录",
            (
                f"版本：{selected.selector}\n"
                f"system：{selected.system_root}\n"
                f"整棵树最近更新：约 {selected.stale_days:.0f} 天前\n"
                f"逻辑大小：{_format_bytes(selected.stats.logical_bytes)}\n\n"
                "该目录已超过 JetBrains 源码使用的 180 天旧版本 shelf life，且没有发现对应的现存安装。\n\n"
                "重要：system 目录可能包含该旧版本的 Local History；删除后不会保留这些恢复记录。"
                "DevClean 不会删除配置目录或用户插件。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新验证完整 180 天边界、安装状态、进程状态和目录身份…")

        def work() -> None:
            try:
                result = cleanup_jetbrains_expired_system_directory(selected)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-JetBrains-leftover-cleanup",
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
            self._status.set(f"JetBrains 过期目录检查失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventories = event.inventories
            self._render()
            self._status.set("JetBrains 版本目录检查完成。")
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"没有报告删除成功：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                before = event.result.before
                self._status.set(
                    f"已确认 {before.selector} 的精确 system 根目录不存在。"
                    f"删除前逻辑统计为 {_format_bytes(before.stats.logical_bytes)}；不承诺等量物理回收。"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for index, item in enumerate(self._inventories):
            installed = "是" if item.installed else "否" if item.installed is False else "不确定"
            state = "厂商过期候选" if item.cleanup_supported else item.reason
            self._tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    item.selector,
                    f"{item.stale_days:.0f} 天前",
                    _format_bytes(item.stats.logical_bytes),
                    installed,
                    state,
                ),
            )
        self._refresh_buttons()


def open_jetbrains_leftover_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _JetBrainsLeftoverMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_jetbrains_leftover_maintenance_dialog"]
