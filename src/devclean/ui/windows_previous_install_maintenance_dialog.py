"""Previous Windows installation maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.windows_previous_install_maintenance import (
    PreviousInstallCleanupResult,
    PreviousInstallInventory,
    cleanup_previous_windows_installation,
    inventory_previous_windows_installation,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: PreviousInstallInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: PreviousInstallCleanupResult | None
    error: str | None = None


class _WindowsPreviousInstallMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("以前的 Windows 安装维护")
        self._window.geometry("900x610")
        self._window.minsize(780, 540)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._inventory: PreviousInstallInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未检查以前的 Windows 安装。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            root,
            text="以前的 Windows 安装维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "这里只处理 Windows 升级后留下的以前系统安装。DevClean 不会直接删除 "
                "Windows.old，也不会自己修改回滚配置；真正清理由 Windows 自己的 "
                "cleanmgr /AUTOCLEAN 完成。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        warning = ttk.LabelFrame(root, text="不可撤销的影响", padding=10)
        warning.pack(fill=tk.X)
        ttk.Label(
            warning,
            text=(
                "删除以前的 Windows 安装后，不能再利用这份升级前系统回滚。微软还明确说明，"
                "Windows.old 在某些升级场景里可能暂时保存没有迁移的个人文件。请先确认"
                "当前 Windows、驱动和软件都正常，并且不需要从 Windows.old 手工取回文件。"
            ),
            wraplength=830,
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        ttk.Label(
            warning,
            text=(
                "这项操作是 USER_REVIEW：不会默认执行，不因为 Windows.old 很大就自动选择，"
                "也不会把是否保留回滚能力交给 AI。"
            ),
            wraplength=830,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(5, 0))

        report = ttk.LabelFrame(root, text="当前状态", padding=10)
        report.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._report_label = ttk.Label(
            report,
            text="尚未检查。",
            wraplength=830,
            justify=tk.LEFT,
        )
        self._report_label.pack(anchor=tk.W)

        ttk.Label(
            root,
            text=(
                "权限边界：微软支持的以前 Windows 安装删除需要管理员权限。DevClean 不会"
                "自动弹 UAC、调用 runas 或创建提权任务。"
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            root,
            textvariable=self._status,
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._inspect_button = ttk.Button(footer, text="检查", command=self._start_inventory)
        self._inspect_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._cleanup_button = ttk.Button(
            footer,
            text="删除以前的 Windows 安装…",
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
        self._inspect_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.cleanup_supported
            and self._inventory.windows_old_identity is not None
        )
        self._cleanup_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在检查系统盘 Windows.old 和 Windows 回滚/清理工具状态…")

        def work() -> None:
            try:
                inventory = inventory_previous_windows_installation()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Windows-previous-install-inventory",
            daemon=True,
        ).start()

    def _confirm_cleanup(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None or not inventory.cleanup_supported:
            return
        size = (
            _format_bytes(inventory.windows_old_logical_bytes)
            if inventory.windows_old_logical_bytes is not None
            else "无法完整读取（不影响安全判定）"
        )
        window = (
            f"{inventory.os_uninstall_window_days} 天（配置窗口，不代表剩余天数）"
            if inventory.os_uninstall_window_days is not None
            else "DISM 未返回可用窗口"
        )
        if not messagebox.askyesno(
            "确认删除以前的 Windows 安装",
            (
                f"Windows.old：{inventory.windows_old}\n"
                f"当前可读逻辑大小：{size}\n"
                f"OS 卸载窗口：{window}\n\n"
                "将调用 Windows 自己的 cleanmgr /AUTOCLEAN 清理升级遗留文件。\n\n"
                "删除后无法再依赖这份以前的 Windows 安装回滚，并且 Windows.old 中仍可能"
                "有你想手工取回的个人文件。此操作不可撤销。\n\n"
                "确认继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set("正在重新验证 Windows.old/cleanmgr 身份并执行 Windows 厂商清理…")

        def work() -> None:
            try:
                result = cleanup_previous_windows_installation(inventory)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Windows-previous-install-cleanup",
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
            self._report_label.configure(text=f"检查失败：{event}")
            self._status.set("没有执行任何清理。")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render_inventory(event.inventory)
            self._set_busy(False)
        elif event.error is not None:
            self._status.set(f"以前的 Windows 安装清理未完成：{event.error}")
            self._set_busy(False)
        elif event.result is not None:
            self._inventory = event.result.after
            self._render_inventory(event.result.after)
            self._status.set(
                "Windows 厂商清理完成；已确认 Windows.old 不再存在。"
                "未把扫描到的逻辑大小宣称为等量物理磁盘回收。"
            )
            self._set_busy(False)

        self._window.after(100, self._poll)

    def _render_inventory(self, inventory: PreviousInstallInventory) -> None:
        old_state = "存在" if inventory.windows_old_identity is not None else "不存在"
        size = (
            _format_bytes(inventory.windows_old_logical_bytes)
            if inventory.windows_old_logical_bytes is not None
            else "不可用"
        )
        bt_state = "存在" if inventory.setup_rollback_present else "未确认存在"
        window = (
            f"{inventory.os_uninstall_window_days} 天（配置值，不是剩余天数）"
            if inventory.os_uninstall_window_days is not None
            else "不可用/当前没有可查询的卸载窗口"
        )
        privilege = "管理员" if inventory.elevated else "未提升"
        cleaner = (
            str(inventory.cleanmgr_identity.path)
            if inventory.cleanmgr_identity is not None
            else "无法验证"
        )
        self._report_label.configure(
            text=(
                f"系统根目录：{inventory.system_root}\n"
                f"Windows.old：{old_state} — {inventory.windows_old}\n"
                f"可读逻辑大小：{size}\n"
                f"$WINDOWS.~BT：{bt_state}\n"
                f"DISM OS 卸载窗口：{window}\n"
                f"当前权限：{privilege}\n"
                f"Windows 清理工具：{cleaner}\n\n"
                f"DevClean 判定：{inventory.reason}\n"
                "执行级别：USER_REVIEW；只使用 Windows 厂商清理，不直接递归删除系统目录。"
            )
        )
        self._status.set("检查完成。")


def open_windows_previous_install_maintenance_dialog(
    parent: tk.Tk | tk.Toplevel,
) -> None:
    _WindowsPreviousInstallMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_windows_previous_install_maintenance_dialog"]
