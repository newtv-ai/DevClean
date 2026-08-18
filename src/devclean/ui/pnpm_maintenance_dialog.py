"""pnpm store garbage-collection UI backed by pnpm's own prune algorithm."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.pnpm_maintenance import (
    PnpmPruneResult,
    PnpmStorageInventory,
    PnpmStoreEntry,
    inventory_pnpm_storage,
    prune_pnpm_store,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: PnpmStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[PnpmPruneResult, ...]
    error: str | None = None


class _PnpmMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("pnpm Store 垃圾收集")
        self._window.geometry("940x610")
        self._window.minsize(800, 520)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 pnpm store…")
        self._inventory: PnpmStorageInventory | None = None
        self._choices: dict[str, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="pnpm Store 垃圾收集",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "这是可以由 DevClean 本地确定的安全清理，不需要 AI。pnpm store prune 会由 pnpm"
                "自己识别系统中没有任何项目引用的包并删除它们；DevClean 不分析也不直接删除"
                "store 内部文件。"
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "pnpm 官方说明该操作不会伤害现有项目。以后切换到旧分支或重新需要已移除包时，"
                "pnpm 会重新下载，因此适合偶尔做一次而不是频繁运行。store 达到 1 GiB 时"
                "DevClean 才默认勾选；这个阈值只衡量收益，不是安全边界。"
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="垃圾收集已勾选 Store",
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
        self._clean_button.configure(
            state=tk.DISABLED if busy or self._inventory is None else tk.NORMAL
        )

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在统计 pnpm store…")

        def work() -> None:
            try:
                inventory = inventory_pnpm_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-pnpm-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[PnpmStoreEntry] = []
        for entry in self._inventory.stores:
            choice = self._choices.get(str(entry.path))
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("pnpm Store 垃圾收集", "没有勾选需要垃圾收集的 pnpm store。")
            return

        self._set_busy(True)
        self._status.set(f"正在通过 pnpm 官方 GC 清理 {len(selected)} 个 store…")

        def work(entries: tuple[PnpmStoreEntry, ...] = tuple(selected)) -> None:
            results: list[PnpmPruneResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(prune_pnpm_store(entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-pnpm-prune", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"pnpm 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                text = f"pnpm 垃圾收集完成，共释放约 {_format_bytes(reclaimed)}"
            else:
                text = (
                    f"已完成 {len(outcome.results)} 个 store 并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}"
                )
            self._status.set(f"{text}；正在重新统计…")
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: PnpmStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.stores if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[str(entry.path)] = choice
            title = f"确定可安全 GC · {_format_bytes(entry.logical_bytes)}"
            row = ttk.LabelFrame(self._rows, text=title, padding=9)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 7))
            self._rows.columnconfigure(0, weight=1)
            ttk.Checkbutton(row, text="运行 prune", variable=choice).grid(
                row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12)
            )
            ttk.Label(
                row,
                text="只删除 pnpm 判定为没有任何项目引用的包，不直接改写 store 内部结构",
                wraplength=740,
                justify=tk.LEFT,
            ).grid(row=0, column=1, sticky="w")
            ttk.Label(row, text=str(entry.path), wraplength=740, justify=tk.LEFT).grid(
                row=1, column=1, sticky="w", pady=(4, 0)
            )
            row.columnconfigure(1, weight=1)

        total = _format_bytes(inventory.total_store_bytes)
        recommended = _format_bytes(inventory.recommended_bytes)
        self._status.set(
            f"已定位 {len(visible)} 个 pnpm store，共 {total}；默认建议检查约 {recommended}。"
        )


def open_pnpm_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _PnpmMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_pnpm_maintenance_dialog"]
