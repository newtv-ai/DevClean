"""Windows Delivery Optimization exact cache maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox, ttk

from devclean.core.delivery_optimization_cache_maintenance import (
    DeliveryOptimizationDeleteResult,
    DeliveryOptimizationEntry,
    DeliveryOptimizationInventory,
    delete_delivery_optimization_cache_file,
    inventory_delivery_optimization_cache,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: DeliveryOptimizationInventory


@dataclass(frozen=True, slots=True)
class _DeleteEvent:
    result: DeliveryOptimizationDeleteResult | None
    error: str | None = None


class _DeliveryOptimizationCacheMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Windows Delivery Optimization 缓存维护")
        self._window.geometry("1160x720")
        self._window.minsize(940, 600)
        self._events: queue.Queue[_InventoryEvent | _DeleteEvent | Exception] = queue.Queue()
        self._inventory: DeliveryOptimizationInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 Delivery Optimization 缓存。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="Windows Delivery Optimization 缓存维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "Delivery Optimization 为 Windows Update、Microsoft Store、Defender、Microsoft 365/Edge 等"
                "受支持内容提供下载和对等缓存。DevClean 不扫描或删除它的内部目录，而是通过 Windows 自己的 "
                "DeliveryOptimization PowerShell 模块按精确 FileId 管理。"
            ),
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 6))
        ttk.Label(
            root,
            text=(
                "Pinned 文件永远保护；Downloading / Complete / Paused 以及未知状态都不可执行。"
                "未 pin 且处于 Caching 的文件，如果已经达到 Windows 自己的 ExpireOn，属于 "
                "DETERMINISTIC_CANDIDATE；仍在保留期内则是 USER_REVIEW。"
            ),
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 9))

        columns = ("cache", "file_size", "status", "expire", "pinned", "caller", "decision")
        self._tree = ttk.Treeview(root, columns=columns, show="tree headings", selectmode="browse")
        self._tree.heading("#0", text="FileId")
        self._tree.heading("cache", text="缓存占用")
        self._tree.heading("file_size", text="文件大小")
        self._tree.heading("status", text="状态")
        self._tree.heading("expire", text="ExpireOn")
        self._tree.heading("pinned", text="Pinned")
        self._tree.heading("caller", text="调用方")
        self._tree.heading("decision", text="当前判定")
        self._tree.column("#0", width=220, stretch=True)
        self._tree.column("cache", width=95, anchor=tk.E, stretch=False)
        self._tree.column("file_size", width=95, anchor=tk.E, stretch=False)
        self._tree.column("status", width=100, stretch=False)
        self._tree.column("expire", width=180, stretch=False)
        self._tree.column("pinned", width=70, anchor=tk.CENTER, stretch=False)
        self._tree.column("caller", width=150, stretch=True)
        self._tree.column("decision", width=330, stretch=True)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda event: self._update_button())

        self._tools_label = ttk.Label(root, text="", wraplength=1110, justify=tk.LEFT)
        self._tools_label.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            root,
            textvariable=self._status,
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))
        ttk.Label(
            root,
            text=(
                "删除前会再次读取同一 FileId 的 status、pin、ExpireOn 和缓存字节并重新确认 Windows PowerShell/"
                "DeliveryOptimization module 文件身份。真正的删除操作只有 "
                "Delete-DeliveryOptimizationCache -FileID <精确 FileId> -Force；永远不会使用 IncludePinnedFiles。"
            ),
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="检查/刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._delete_button = ttk.Button(
            footer,
            text="删除选中的精确缓存文件…",
            command=self._confirm_delete,
            state=tk.DISABLED,
        )
        self._delete_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()
        self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._update_button()

    def _selected(self) -> DeliveryOptimizationEntry | None:
        if self._inventory is None:
            return None
        selected = self._tree.selection()
        if len(selected) != 1:
            return None
        file_id = selected[0]
        matches = [entry for entry in self._inventory.entries if entry.file_id == file_id]
        return matches[0] if len(matches) == 1 else None

    def _update_button(self) -> None:
        selected = self._selected()
        enabled = not self._busy and selected is not None and selected.deletion_supported
        self._delete_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过 Windows DeliveryOptimization 模块读取缓存状态…")

        def work() -> None:
            try:
                inventory = inventory_delivery_optimization_cache()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-DeliveryOptimization-inventory",
            daemon=True,
        ).start()

    def _confirm_delete(self) -> None:
        if self._busy:
            return
        entry = self._selected()
        inventory = self._inventory
        if entry is None or inventory is None or not entry.deletion_supported:
            return
        expiry = _format_datetime(entry.expire_on)
        warning = (
            "这个缓存已经达到 Windows 自己的 ExpireOn；删除属于 vendor-expired cache。"
            if entry.decision_class == "DETERMINISTIC_CANDIDATE"
            else (
                "这个缓存仍在 Windows 的保留期内。删除后内容可再次下载，但会失去当前本机/对等缓存价值，"
                "可能增加后续网络流量。"
            )
        )
        if not messagebox.askyesno(
            "确认删除 Delivery Optimization 缓存文件",
            (
                "将通过 Windows 自己的 cmdlet 删除一个精确 Delivery Optimization FileId。\n\n"
                f"FileId：{entry.file_id}\n"
                f"状态：{entry.status}\n"
                f"缓存占用：{_format_bytes(entry.cache_bytes)}\n"
                f"内容大小：{_format_bytes(entry.file_size)}\n"
                f"ExpireOn：{expiry}\n"
                f"Pinned：{'是' if entry.pinned else '否'}\n"
                f"判定：{entry.decision_class}\n\n"
                f"{warning}\n\n"
                "DevClean 不会删除 pinned 文件，也不会清空整个 Delivery Optimization cache。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set(
            "正在重新验证 FileId、pin、ExpireOn、状态和 Windows 模块身份，然后执行精确删除…"
        )

        def work() -> None:
            try:
                result = delete_delivery_optimization_cache_file(entry, inventory)
            except Exception as error:
                self._events.put(_DeleteEvent(None, str(error)))
            else:
                self._events.put(_DeleteEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-DeliveryOptimization-delete",
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
            self._inventory = None
            self._clear_tree()
            self._tools_label.configure(text="")
            self._status.set(f"Delivery Optimization 检查失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Delivery Optimization 缓存删除未完成：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._status.set(
                    f"精确 FileId 已删除。重新统计后 Delivery Optimization 报告的缓存总量减少约 "
                    f"{_format_bytes(result.observed_cache_delta)}；这是 vendor 逻辑缓存变化，不等同于物理磁盘承诺。"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _render(self, inventory: DeliveryOptimizationInventory) -> None:
        self._clear_tree()
        for entry in inventory.entries:
            self._tree.insert(
                "",
                tk.END,
                iid=entry.file_id,
                text=entry.file_id,
                values=(
                    _format_bytes(entry.cache_bytes),
                    _format_bytes(entry.file_size),
                    entry.status,
                    _format_datetime(entry.expire_on),
                    "是" if entry.pinned else "否",
                    entry.caller or "—",
                    f"{entry.decision_class}：{entry.reason}",
                ),
            )
        elevation = "管理员" if inventory.elevated else "非管理员（只报告）"
        self._tools_label.configure(
            text=(
                f"执行环境：{elevation}\n"
                f"PowerShell：{inventory.powershell.path}\n"
                f"DeliveryOptimization module：{inventory.module_manifest.path}"
            )
        )
        supported = sum(entry.deletion_supported for entry in inventory.entries)
        self._status.set(
            f"共识别 {len(inventory.entries)} 个 Delivery Optimization 条目，当前缓存约 "
            f"{_format_bytes(inventory.cache_bytes)}；{supported} 个条目可在再次验证后执行精确删除。"
        )


def open_delivery_optimization_cache_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _DeliveryOptimizationCacheMaintenanceDialog(parent).show()


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "未提供/无限期"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_delivery_optimization_cache_maintenance_dialog"]
