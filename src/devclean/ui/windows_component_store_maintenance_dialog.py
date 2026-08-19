"""Windows component-store maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.windows_component_store_maintenance import (
    ComponentStoreCleanupResult,
    ComponentStoreInventory,
    ComponentStoreReport,
    cleanup_windows_component_store,
    inventory_windows_component_store,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: ComponentStoreInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: ComponentStoreCleanupResult | None
    error: str | None = None


class _WindowsComponentStoreMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Windows 组件存储维护")
        self._window.geometry("980x680")
        self._window.minsize(820, 580)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._inventory: ComponentStoreInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未运行 DISM 组件存储分析。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="Windows 组件存储维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "这里不会扫描或删除 WinSxS 文件。DevClean 只调用 Windows 自己的 DISM "
                "组件存储分析，并且只有当 DISM 明确建议清理时，才允许你手动确认"
                " StartComponentCleanup。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        warning = ttk.LabelFrame(root, text="为什么需要你确认", padding=10)
        warning.pack(fill=tk.X)
        ttk.Label(
            warning,
            text=(
                "Windows 的自动 StartComponentCleanup 任务通常会给旧组件版本保留 30 天"
                "宽限期；手动 DISM StartComponentCleanup 会立即删除这些旧版本。"
                "这仍是微软支持的维护操作，但会用一部分回滚余量换取更快的磁盘清理，"
                "因此 DevClean 把它放在 USER_REVIEW，而不是默认清理。"
            ),
            wraplength=910,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        ttk.Label(
            warning,
            text=(
                "DevClean 永远不会添加 /ResetBase。微软明确说明该选项会让当前已安装的"
                "更新包无法卸载；这里没有所谓“深度清理”开关。"
            ),
            wraplength=910,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))

        report_frame = ttk.LabelFrame(root, text="DISM 分析结果", padding=10)
        report_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._report_label = ttk.Label(
            report_frame,
            text="尚未分析。",
            wraplength=910,
            justify=tk.LEFT,
        )
        self._report_label.pack(anchor=tk.W)
        self._raw_text = tk.Text(report_frame, height=12, wrap=tk.NONE)
        self._raw_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        self._raw_text.configure(state=tk.DISABLED)

        ttk.Label(
            root,
            text=(
                "权限边界：DISM 在线组件维护需要管理员权限。DevClean 不会自动弹 UAC、"
                "不会调用 runas，也不会创建提权任务；如果当前进程未提升，只显示说明。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            root,
            textvariable=self._status,
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._analyze_button = ttk.Button(
            footer,
            text="运行 DISM 分析",
            command=self._start_inventory,
        )
        self._analyze_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._cleanup_button = ttk.Button(
            footer,
            text="手动执行组件存储清理…",
            command=self._confirm_cleanup,
            state=tk.DISABLED,
        )
        self._cleanup_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._analyze_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        report = self._current_report()
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.elevated
            and self._inventory.cleanup_supported
            and report is not None
            and report.cleanup_recommended
        )
        self._cleanup_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _current_report(self) -> ComponentStoreReport | None:
        if self._inventory is None:
            return None
        return self._inventory.report

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在运行 DISM /AnalyzeComponentStore；大型组件存储可能需要几分钟…")

        def work() -> None:
            try:
                inventory = inventory_windows_component_store()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Windows-component-store-inventory",
            daemon=True,
        ).start()

    def _confirm_cleanup(self) -> None:
        if self._busy:
            return
        report = self._current_report()
        if (
            report is None
            or self._inventory is None
            or not self._inventory.cleanup_supported
            or not report.cleanup_recommended
        ):
            return
        packages = (
            str(report.reclaimable_packages)
            if report.reclaimable_packages is not None
            else "DISM 未提供可解析数量"
        )
        if not messagebox.askyesno(
            "确认手动组件存储清理",
            (
                "将执行微软 DISM 的手动 StartComponentCleanup。\n\n"
                f"Windows 映像：{report.image_version}\n"
                f"DISM 报告的可回收包：{packages}\n\n"
                "重要：手动执行会跳过自动维护通常使用的 30 天旧组件宽限期。"
                "DevClean 不会使用 /ResetBase，也不会删除 WinSxS 文件。\n\n"
                "确定用这部分回滚余量换取当前组件存储清理吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set(
            "正在重新分析并验证 DISM/Windows 映像身份，然后执行 StartComponentCleanup；请勿关闭程序…"
        )

        def work() -> None:
            try:
                result = cleanup_windows_component_store(report)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Windows-component-store-cleanup",
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
            self._report_label.configure(text=f"DISM 分析失败：{event}")
            self._set_raw("")
            self._status.set("没有执行任何组件存储清理。")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render_inventory(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"组件存储清理未完成：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._inventory = ComponentStoreInventory(
                    elevated=True,
                    report=result.after,
                    cleanup_supported=result.after.cleanup_recommended,
                    reason=(
                        "清理后 DISM 仍建议组件存储维护；DevClean 不会通过更强参数强制扩大范围"
                        if result.after.cleanup_recommended
                        else "清理完成；DISM 当前不再建议组件存储清理"
                    ),
                )
                self._render_inventory(self._inventory)
                delta = result.reported_size_delta_bytes
                if delta is None:
                    evidence = "DISM 没有提供可比较的组件存储大小字段。"
                else:
                    evidence = (
                        f"DISM 报告的组件存储大小减少约 {_format_bytes(delta)}；"
                        "这不是对物理空闲磁盘空间的承诺。"
                    )
                self._status.set(f"StartComponentCleanup 完成。{evidence}")
                self._set_busy(False)
        self._window.after(100, self._poll)

    def _render_inventory(self, inventory: ComponentStoreInventory) -> None:
        report = inventory.report
        if not inventory.elevated or report is None:
            self._report_label.configure(
                text=(
                    "当前 DevClean 未以管理员身份运行。组件存储没有被分析，也没有执行任何维护。\n"
                    f"原因：{inventory.reason}\n"
                    "如确实需要此功能，请由你自己明确关闭并“以管理员身份运行” DevClean。"
                )
            )
            self._set_raw("")
            self._status.set("只读说明；DevClean 不会自动提升权限。")
            return

        size = (
            _format_bytes(report.actual_size_bytes)
            if report.actual_size_bytes is not None
            else "DISM 字段无法安全解析"
        )
        packages = (
            str(report.reclaimable_packages)
            if report.reclaimable_packages is not None
            else "DISM 字段无法安全解析"
        )
        recommendation = "是" if report.cleanup_recommended else "否"
        self._report_label.configure(
            text=(
                f"DISM 版本：{report.dism_version}\n"
                f"Windows 映像版本：{report.image_version}\n"
                f"DISM 报告的实际组件存储大小：{size}\n"
                f"可回收包数量：{packages}\n"
                f"DISM 建议组件存储清理：{recommendation}\n\n"
                f"DevClean 判定：{inventory.reason}\n"
                "执行级别：USER_REVIEW；不会默认执行，也不发送 AI。"
            )
        )
        self._set_raw(report.raw_output)
        self._status.set("DISM 分析完成。")

    def _set_raw(self, text: str) -> None:
        self._raw_text.configure(state=tk.NORMAL)
        self._raw_text.delete("1.0", tk.END)
        if text:
            self._raw_text.insert("1.0", text)
        self._raw_text.configure(state=tk.DISABLED)


def open_windows_component_store_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WindowsComponentStoreMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_windows_component_store_maintenance_dialog"]
