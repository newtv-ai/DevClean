"""Windows crash-dump review and exact permanent-removal UI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import UTC, datetime
from tkinter import messagebox, ttk

from devclean.core.windows_crash_dump_maintenance import (
    WindowsCrashDumpDeleteResult,
    WindowsCrashDumpEntry,
    WindowsCrashDumpInventory,
    WindowsCrashDumpKind,
    delete_windows_crash_dump,
    inventory_windows_crash_dumps,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: WindowsCrashDumpInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[WindowsCrashDumpDeleteResult, ...]
    error: str | None = None


class _WindowsCrashDumpMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Windows 崩溃转储维护")
        self._window.geometry("1120x720")
        self._window.minsize(900, 600)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._inventory: WindowsCrashDumpInventory | None = None
        self._rows: dict[str, WindowsCrashDumpEntry] = {}
        self._busy = False
        self._status = tk.StringVar(value="正在读取 Windows 崩溃转储配置…")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Windows 崩溃转储维护", font=("Segoe UI", 13, "bold")).pack(
            anchor=tk.W
        )
        ttk.Label(
            root,
            text=(
                "崩溃转储不是缓存，而是用于 WinDbg、驱动/应用故障排查和技术支持的诊断证据。"
                "DevClean 只识别 Windows 当前 CrashControl / WER LocalDumps 配置能够证明的"
                "精确转储文件，并全部放在 USER_REVIEW：不会按年龄或大小自动删除，也不发送 AI。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        info = ttk.LabelFrame(root, text="审计边界", padding=8)
        info.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            info,
            text=(
                "内核/完整/自动转储使用 CrashControl 的精确 DumpFile；小型内核转储只检查"
                " MinidumpDir 的直接 .dmp 文件；用户模式转储只检查已确认 LocalDumps "
                "DumpFolder 的直接 .dmp 文件。自定义环境变量路径、网络/可移动存储、reparse、"
                "硬链接以及嵌套任意目录都不会获得删除权限。内核转储删除要求你自己以管理员"
                "身份启动 DevClean；程序不会自动提权。"
            ),
            wraplength=1050,
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
            columns=("kind", "size", "modified", "status", "source", "path"),
            show="headings",
            selectmode="extended",
        )
        for column, text in (
            ("kind", "类型"),
            ("size", "逻辑大小"),
            ("modified", "最后写入"),
            ("status", "处理级别"),
            ("source", "Windows 配置来源"),
            ("path", "路径"),
        ):
            self._tree.heading(column, text=text)
        self._tree.column("kind", width=120, stretch=False)
        self._tree.column("size", width=100, anchor=tk.E, stretch=False)
        self._tree.column("modified", width=150, stretch=False)
        self._tree.column("status", width=150, stretch=False)
        self._tree.column("source", width=250, stretch=False)
        self._tree.column("path", width=520, stretch=True)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._warnings = ttk.Label(root, text="", wraplength=1080, justify=tk.LEFT)
        self._warnings.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=1080, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(5, 0)
        )

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._delete_button = ttk.Button(
            footer,
            text="永久删除选中的转储…",
            command=self._start_cleanup,
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
        self._status.set("正在读取 CrashControl / WER LocalDumps 并验证精确文件身份…")

        def work() -> None:
            try:
                inventory = inventory_windows_crash_dumps()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Windows-crash-dump-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected = [self._rows[item] for item in self._tree.selection() if item in self._rows]
        if not selected:
            messagebox.showinfo(
                "Windows 崩溃转储维护",
                "请先选择一个或多个崩溃转储。",
                parent=self._window,
            )
            return
        blocked = [entry for entry in selected if not entry.deletion_supported]
        if blocked:
            messagebox.showwarning(
                "当前选择包含不可执行项",
                "至少一个所选转储当前只能查看：\n\n" + "\n".join(entry.reason for entry in blocked[:3]),
                parent=self._window,
            )
            return
        total = sum(entry.logical_bytes for entry in selected)
        kinds = ", ".join(sorted({_kind_label(entry.kind) for entry in selected}))
        if not messagebox.askyesno(
            "确认永久删除诊断转储",
            (
                f"将永久删除 {len(selected)} 个 Windows 崩溃转储，逻辑大小约 {_format_bytes(total)}。\n"
                f"类型：{kinds}\n\n"
                "删除后将失去这些崩溃时刻的诊断证据，无法再用它们进行 WinDbg/驱动/应用故障分析。"
                "DevClean 不会修改未来的转储生成配置。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在逐个重新验证并精确删除 {len(selected)} 个已确认转储…")

        def work(entries: tuple[WindowsCrashDumpEntry, ...] = tuple(selected)) -> None:
            results: list[WindowsCrashDumpDeleteResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(delete_windows_crash_dump(entry))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-Windows-crash-dump-cleanup",
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
            self._status.set(f"崩溃转储检查失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            removed = sum(result.logical_bytes_removed for result in event.results)
            if event.error is None:
                self._status.set(
                    f"已删除 {len(event.results)} 个转储；移除的逻辑文件大小约 {_format_bytes(removed)}。"
                    "正在重新检查…"
                )
            else:
                self._status.set(
                    f"已删除 {len(event.results)} 个转储后停止：{event.error}。正在重新检查…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: WindowsCrashDumpInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows.clear()
        for index, entry in enumerate(inventory.entries):
            item_id = f"dump-{index}"
            self._rows[item_id] = entry
            status = "USER_REVIEW" if entry.deletion_supported else "REPORT_ONLY"
            self._tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    _kind_label(entry.kind),
                    _format_bytes(entry.logical_bytes),
                    _format_time(entry.last_write_time_ns),
                    status,
                    entry.source,
                    str(entry.path),
                ),
            )
        self._warnings.configure(
            text=("注意：" + "；".join(inventory.warnings)) if inventory.warnings else ""
        )
        executable = sum(1 for entry in inventory.entries if entry.deletion_supported)
        self._status.set(
            f"找到 {len(inventory.entries)} 个精确诊断转储，逻辑大小约 "
            f"{_format_bytes(inventory.logical_bytes)}；当前可由用户确认删除 {executable} 个。"
        )


def open_windows_crash_dump_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WindowsCrashDumpMaintenanceDialog(parent).show()


def _kind_label(kind: WindowsCrashDumpKind) -> str:
    return {
        WindowsCrashDumpKind.KERNEL_MEMORY: "内核/完整转储",
        WindowsCrashDumpKind.KERNEL_SMALL: "小型内核转储",
        WindowsCrashDumpKind.USER_MODE: "用户模式转储",
    }[kind]


def _format_time(value_ns: int) -> str:
    if value_ns <= 0:
        return "未知"
    return datetime.fromtimestamp(value_ns / 1_000_000_000, tz=UTC).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_windows_crash_dump_maintenance_dialog"]
