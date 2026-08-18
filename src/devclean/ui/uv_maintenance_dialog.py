"""uv cache maintenance UI backed only by uv's safe prune operation."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.uv_maintenance import (
    UvCacheEntry,
    UvPruneResult,
    UvStorageInventory,
    inventory_uv_storage,
    prune_uv_cache,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: UvStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[UvPruneResult, ...]
    error: str | None = None


class _UvMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("uv 缓存垃圾收集")
        self._window.geometry("940x610")
        self._window.minsize(800, 520)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 uv cache…")
        self._inventory: UvStorageInventory | None = None
        self._choices: dict[str, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="uv 缓存垃圾收集",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "uv 官方明确说明不能直接修改 cache 内部文件，并提供 `uv cache prune` 安全删除"
                "未使用的 cache 项和可按需重建的集中式项目环境。因此这是 DevClean 可以本地"
                "确定的清理，不需要 AI。"
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "DevClean 会先重新确认当前有效 cache 路径，再调用 uv 自己的 prune。达到 512 MiB"
                "时默认勾选；较小 cache 仍然可以安全 prune，但通常保留它更有利于后续下载和构建"
                "性能。该阈值只衡量磁盘收益。"
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
            text="垃圾收集已勾选 Cache",
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
        self._status.set("正在统计 uv cache…")

        def work() -> None:
            try:
                inventory = inventory_uv_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-uv-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[UvCacheEntry] = []
        for entry in self._inventory.caches:
            choice = self._choices.get(str(entry.path))
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("uv 缓存垃圾收集", "没有勾选需要垃圾收集的 uv cache。")
            return

        self._set_busy(True)
        self._status.set(f"正在通过 uv 官方 prune 清理 {len(selected)} 处 cache…")

        def work(entries: tuple[UvCacheEntry, ...] = tuple(selected)) -> None:
            results: list[UvPruneResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(prune_uv_cache(entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-uv-prune", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"uv 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                text = f"uv 垃圾收集完成，共释放约 {_format_bytes(reclaimed)}"
            else:
                text = (
                    f"已完成 {len(outcome.results)} 处 cache 并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}"
                )
            self._status.set(f"{text}；正在重新统计…")
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: UvStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.caches if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[str(entry.path)] = choice
            title = f"确定可安全 prune · {_format_bytes(entry.logical_bytes)}"
            row = ttk.LabelFrame(self._rows, text=title, padding=9)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 7))
            self._rows.columnconfigure(0, weight=1)
            ttk.Checkbutton(row, text="运行 prune", variable=choice).grid(
                row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12)
            )
            ttk.Label(
                row,
                text="由 uv 删除未使用 cache；集中式项目环境需要时会自动重建",
                wraplength=740,
                justify=tk.LEFT,
            ).grid(row=0, column=1, sticky="w")
            ttk.Label(row, text=str(entry.path), wraplength=740, justify=tk.LEFT).grid(
                row=1, column=1, sticky="w", pady=(4, 0)
            )
            row.columnconfigure(1, weight=1)

        total = _format_bytes(inventory.total_cache_bytes)
        recommended = _format_bytes(inventory.recommended_bytes)
        self._status.set(
            f"已定位 {len(visible)} 处 uv cache，共 {total}；默认建议检查约 {recommended}。"
        )


def open_uv_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _UvMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_uv_maintenance_dialog"]
