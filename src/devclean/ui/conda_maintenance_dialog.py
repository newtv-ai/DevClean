"""Conda cache maintenance UI for vendor-owned safe cache targets."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.conda_maintenance import (
    CondaCleanResult,
    CondaPackageCacheEntry,
    CondaStorageInventory,
    clean_conda_package_cache,
    inventory_conda_storage,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: CondaStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[CondaCleanResult, ...]
    error: str | None = None


class _CondaMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Conda 安全缓存维护")
        self._window.geometry("960x630")
        self._window.minsize(820, 540)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Conda package cache…")
        self._inventory: CondaStorageInventory | None = None
        self._choices: dict[str, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Conda 安全缓存维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Conda 的 pkgs 目录混合了下载缓存和可能被环境链接使用的 extracted packages。"
                "DevClean 能确定 tarball 与 index cache 可以通过 Conda 官方命令安全清理，"
                "所以不需要 AI；但不会删除 extracted packages，也不会调用风险更高的"
                " `--packages`、`--all` 或 `--force-pkgs-dirs`。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "DevClean 会把每个已审计 package cache 单独交给 Conda，在执行前先让同一个"
                " Conda 可执行文件通过 `conda info --json` 再次确认该 cache。目录达到 1 GiB"
                " 才默认勾选；这个阈值只衡量是否值得运行，不改变安全边界。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="清理已勾选缓存",
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
        self._status.set("正在统计 Conda package cache…")

        def work() -> None:
            try:
                inventory = inventory_conda_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Conda-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[CondaPackageCacheEntry] = []
        for entry in self._inventory.package_caches:
            choice = self._choices.get(str(entry.path))
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("Conda 安全缓存维护", "没有勾选需要清理的 Conda cache。")
            return

        self._set_busy(True)
        self._status.set(f"正在通过 Conda 官方命令清理 {len(selected)} 处缓存…")

        def work(entries: tuple[CondaPackageCacheEntry, ...] = tuple(selected)) -> None:
            results: list[CondaCleanResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(clean_conda_package_cache(entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-Conda-cleanup", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"Conda 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                text = f"Conda 安全缓存清理完成，共释放约 {_format_bytes(reclaimed)}"
            else:
                text = (
                    f"已完成 {len(outcome.results)} 处 cache 并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}"
                )
            self._status.set(f"{text}；正在重新统计…")
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: CondaStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.package_caches if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[str(entry.path)] = choice
            title = f"确定可安全清理 · {_format_bytes(entry.logical_bytes)}"
            row = ttk.LabelFrame(self._rows, text=title, padding=9)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 7))
            self._rows.columnconfigure(0, weight=1)
            ttk.Checkbutton(row, text="清理", variable=choice).grid(
                row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12)
            )
            ttk.Label(row, text=entry.reason, wraplength=760, justify=tk.LEFT).grid(
                row=0, column=1, sticky="w"
            )
            ttk.Label(row, text=str(entry.path), wraplength=760, justify=tk.LEFT).grid(
                row=1, column=1, sticky="w", pady=(4, 0)
            )
            row.columnconfigure(1, weight=1)

        total = _format_bytes(inventory.total_package_cache_bytes)
        recommended = _format_bytes(inventory.recommended_bytes)
        self._status.set(
            f"已定位 {len(visible)} 处 Conda package cache，共 {total}；"
            f"默认建议检查约 {recommended}。"
        )


def open_conda_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _CondaMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_conda_maintenance_dialog"]
