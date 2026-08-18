"""Conan 2 deterministic cache-clean dialog."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.conan_maintenance import (
    ConanCacheCleanResult,
    ConanStorageInventory,
    clean_conan_cache,
    inventory_conan_storage,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: ConanStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: ConanCacheCleanResult | None
    error: str | None = None


class _ConanMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Conan 2 缓存维护")
        self._window.geometry("900x500")
        self._window.minsize(760, 440)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在通过 Conan 2 定位本地 cache...")
        self._choice = tk.BooleanVar(value=False)
        self._inventory: ConanStorageInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Conan 2 安全缓存维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Conan 官方把 package cache 存储视为只读, 并提供 `conan cache clean` "
                "删除 source/build/download/temp 等 non-critical 目录. DevClean 不直接修改"
                " .conan2 内部结构, 也不会删除已安装的 package artifacts."
            ),
            wraplength=860,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 12))

        self._details = ttk.LabelFrame(container, text="当前 Conan 2 cache", padding=10)
        self._details.pack(fill=tk.BOTH, expand=True)
        self._detail_text = ttk.Label(
            self._details,
            text="正在读取...",
            wraplength=820,
            justify=tk.LEFT,
        )
        self._detail_text.pack(anchor=tk.W)
        self._check = ttk.Checkbutton(
            self._details,
            text="运行 Conan 官方 non-critical cache clean",
            variable=self._choice,
            state=tk.DISABLED,
        )
        self._check.pack(anchor=tk.W, pady=(12, 0))

        ttk.Label(
            container,
            text=(
                "达到 1 GiB 时默认勾选仅表示值得检查. 这里显示的是整个 Conan home 大小, "
                "实际可回收量可能更小; 安全边界来自 Conan 自己的 cache clean 语义."
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
        self._clean_button = ttk.Button(
            footer,
            text="执行安全清理",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._clean_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._refresh_button = ttk.Button(
            footer,
            text="重新统计",
            command=self._start_inventory,
        )
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
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
        available = self._inventory is not None and self._inventory.exists
        state = tk.DISABLED if busy or not available else tk.NORMAL
        self._clean_button.configure(state=state)
        self._check.configure(state=state)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过 Conan 2 定位并统计 cache...")

        def work() -> None:
            try:
                inventory = inventory_conan_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Conan-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None or not inventory.exists:
            return
        if not self._choice.get():
            messagebox.showinfo("Conan 2 缓存维护", "请先勾选 Conan 2 安全缓存清理.")
            return
        self._set_busy(True)
        self._status.set("正在执行 conan cache clean, 期间不会进行 raw delete...")

        def work(home=inventory.home) -> None:  # type: ignore[no-untyped-def]
            try:
                result = clean_conan_cache(home)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(target=work, name="DevClean-Conan-cleanup", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._inventory = None
            self._detail_text.configure(text=f"无法定位 Conan 2 cache: {outcome}")
            self._status.set("Conan 2 不可用或版本不受支持.")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"Conan 安全清理失败: {outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                reclaimed = _format_bytes(outcome.result.reclaimed_bytes)
                self._status.set(f"Conan 安全清理完成, 本次释放约 {reclaimed}. 正在重新统计...")
                self._busy = False
                self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: ConanStorageInventory) -> None:
        self._choice.set(inventory.recommended)
        self._detail_text.configure(
            text=(
                f"版本: {inventory.version}\n"
                f"Home: {inventory.home}\n"
                f"当前总占用: {_format_bytes(inventory.logical_bytes)}\n\n"
                f"判定: {inventory.reason}"
            )
        )
        if inventory.exists:
            selected = "已默认勾选" if inventory.recommended else "未默认勾选"
            self._status.set(f"Conan 2 cache 已定位, {selected}.")
        else:
            self._status.set("Conan 2 home 已定位, 但目录当前不存在.")


def open_conan_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _ConanMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_conan_maintenance_dialog"]
