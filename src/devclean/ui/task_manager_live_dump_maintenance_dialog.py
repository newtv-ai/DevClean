"""Task Manager live-kernel dump USER_REVIEW maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import UTC, datetime
from tkinter import messagebox, ttk

from devclean.core.task_manager_live_dump_maintenance import (
    TaskManagerLiveDumpDeleteResult,
    TaskManagerLiveDumpEntry,
    TaskManagerLiveDumpInventory,
    delete_task_manager_live_kernel_dump,
    inventory_task_manager_live_kernel_dumps,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: TaskManagerLiveDumpInventory


@dataclass(frozen=True, slots=True)
class _DeleteEvent:
    results: tuple[TaskManagerLiveDumpDeleteResult, ...]
    error: str | None = None


class _TaskManagerLiveDumpMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Task Manager 实时内核转储维护")
        self._window.geometry("1060x660")
        self._window.minsize(850, 560)
        self._events: queue.Queue[_InventoryEvent | _DeleteEvent | Exception] = queue.Queue()
        self._inventory: TaskManagerLiveDumpInventory | None = None
        self._rows: dict[str, TaskManagerLiveDumpEntry] = {}
        self._busy = False
        self._status = tk.StringVar(value="正在定位当前用户的 Task Manager LiveKernelDumps…")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="Task Manager 实时内核转储维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "这些 .dmp 是你通过任务管理器主动生成的内核诊断快照，不是缓存。DevClean "
                "只检查 Microsoft 文档指定的当前用户 LiveKernelDumps 精确目录，并把每个文件"
                "放在 USER_REVIEW；不会因为文件很旧或很大而自动删除。"
            ),
            wraplength=1020,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        boundary = ttk.LabelFrame(root, text="安全边界", padding=8)
        boundary.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            boundary,
            text=(
                "当前用户 Local AppData 通过 Windows Known Folder API 获取，而不是信任可被外部"
                "修改的 LOCALAPPDATA 环境变量。只识别 LiveKernelDumps 根下的直接 .dmp 文件；"
                "不递归、不碰非 .dmp 文件。任务管理器的用户模式 dump 位于混合的 Temp 目录，"
                "本功能明确不扫描也不删除它们。"
            ),
            wraplength=990,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)

        toolbar = ttk.Frame(root)
        toolbar.pack(fill=tk.X, pady=(0, 8))
        self._refresh_button = ttk.Button(toolbar, text="重新检查", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT)

        table_frame = ttk.Frame(root)
        table_frame.pack(fill=tk.BOTH, expand=True)
        self._tree = ttk.Treeview(
            table_frame,
            columns=("size", "modified", "status", "path"),
            show="headings",
            selectmode="extended",
        )
        for column, text in (
            ("size", "逻辑大小"),
            ("modified", "最后写入"),
            ("status", "处理级别"),
            ("path", "路径"),
        ):
            self._tree.heading(column, text=text)
        self._tree.column("size", width=110, anchor=tk.E, stretch=False)
        self._tree.column("modified", width=160, stretch=False)
        self._tree.column("status", width=130, stretch=False)
        self._tree.column("path", width=620, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._root_label = ttk.Label(root, text="", wraplength=1020, justify=tk.LEFT)
        self._root_label.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=1020, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(5, 0)
        )

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._delete_button = ttk.Button(
            footer,
            text="永久删除选中的转储…",
            command=self._start_delete,
            state=tk.DISABLED,
        )
        self._delete_button.pack(side=tk.RIGHT, padx=(8, 0))
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
        enabled = not busy and self._inventory is not None and bool(self._inventory.entries)
        self._delete_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过 Known Folder API 定位并验证 Task Manager 转储目录…")

        def work() -> None:
            try:
                inventory = inventory_task_manager_live_kernel_dumps()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-TaskManager-live-dump-inventory",
            daemon=True,
        ).start()

    def _start_delete(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected = [self._rows[item] for item in self._tree.selection() if item in self._rows]
        if not selected:
            messagebox.showinfo(
                "Task Manager 实时内核转储维护",
                "请先选择一个或多个实时内核转储。",
                parent=self._window,
            )
            return
        total = sum(entry.logical_bytes for entry in selected)
        if not messagebox.askyesno(
            "确认永久删除诊断转储",
            (
                f"将永久删除 {len(selected)} 个任务管理器实时内核转储，逻辑大小约 "
                f"{_format_bytes(total)}。\n\n"
                "这些文件可能是内核、驱动或硬件故障的唯一诊断证据。删除后无法再用它们进行"
                " WinDbg/支持分析。DevClean 不会修改任务管理器以后生成 dump 的设置。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在逐个重新验证并精确删除 {len(selected)} 个已确认转储…")

        def work(entries: tuple[TaskManagerLiveDumpEntry, ...] = tuple(selected)) -> None:
            results: list[TaskManagerLiveDumpDeleteResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(delete_task_manager_live_kernel_dump(entry))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_DeleteEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-TaskManager-live-dump-delete",
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
            self._status.set(f"Task Manager 实时内核转储检查失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            removed = sum(result.logical_bytes_removed for result in event.results)
            if event.error is None:
                self._status.set(
                    f"已删除 {len(event.results)} 个转储；移除的逻辑文件大小约 "
                    f"{_format_bytes(removed)}。正在重新检查…"
                )
            else:
                self._status.set(
                    f"已删除 {len(event.results)} 个转储后停止：{event.error}。正在重新检查…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: TaskManagerLiveDumpInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows.clear()
        for index, entry in enumerate(inventory.entries):
            item_id = f"task-dump-{index}"
            self._rows[item_id] = entry
            self._tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    _format_bytes(entry.logical_bytes),
                    _format_time(entry.last_write_time_ns),
                    "USER_REVIEW",
                    str(entry.path),
                ),
            )
        self._root_label.configure(
            text=f"Microsoft 文档根：{inventory.root}"
            + (f"\n只读警告：{inventory.warning}" if inventory.warning else "")
        )
        self._status.set(
            f"找到 {len(inventory.entries)} 个任务管理器实时内核转储，逻辑大小约 "
            f"{_format_bytes(inventory.logical_bytes)}。"
        )


def open_task_manager_live_dump_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _TaskManagerLiveDumpMaintenanceDialog(parent).show()


def _format_time(value_ns: int) -> str:
    if value_ns <= 0:
        return "未知"
    return (
        datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_task_manager_live_dump_maintenance_dialog"]
