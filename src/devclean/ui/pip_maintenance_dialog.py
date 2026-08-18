"""pip cache maintenance UI backed only by pip's own cache command."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.pip_maintenance import (
    PipCacheEntry,
    PipCachePurgeResult,
    PipStorageInventory,
    inventory_pip_storage,
    purge_pip_cache,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: PipStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[PipCachePurgeResult, ...]
    error: str | None = None


class _PipMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("pip 缓存维护")
        self._window.geometry("940x610")
        self._window.minsize(800, 520)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 pip 缓存…")
        self._inventory: PipStorageInventory | None = None
        self._choices: dict[str, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="pip 缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "pip 的 HTTP 和 wheel 数据是明确缓存，不需要 AI 判断。DevClean 不再直接递归"
                "删除 pip 的内部目录，而是先让 pip 自己确认目标 cache，再执行 `pip cache purge`。"
                "缓存较小时保留通常更省下载/编译时间，因此只有达到 512 MiB 才默认勾选。"
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "清理不会卸载已经安装的 Python 包，但之后安装相同依赖时可能需要重新下载或"
                "重新构建 wheel。自定义 cache 路径也只通过 pip 官方命令处理，不直接删目录。"
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
            text="清理已勾选",
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
        self._status.set("正在统计 pip 缓存…")

        def work() -> None:
            try:
                inventory = inventory_pip_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-pip-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[PipCacheEntry] = []
        for entry in self._inventory.caches:
            choice = self._choices.get(str(entry.path))
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("pip 缓存维护", "没有勾选需要清理的 pip 缓存。")
            return

        self._set_busy(True)
        self._status.set(f"正在通过 pip 官方命令清理 {len(selected)} 处缓存…")

        def work(entries: tuple[PipCacheEntry, ...] = tuple(selected)) -> None:
            results: list[PipCachePurgeResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(purge_pip_cache(entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-pip-cleanup", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"pip 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                text = f"pip 官方清理完成，共释放约 {_format_bytes(reclaimed)}"
            else:
                text = (
                    f"已完成 {len(outcome.results)} 项并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}"
                )
            self._status.set(f"{text}；正在重新统计…")
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: PipStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.caches if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[str(entry.path)] = choice
            origin = "自定义 cache" if entry.custom else "默认 cache"
            title = f"确定可清理 · {origin} · {_format_bytes(entry.logical_bytes)}"
            row = ttk.LabelFrame(self._rows, text=title, padding=9)
            row.grid(row=row_index, column=0, sticky="ew", pady=(0, 7))
            self._rows.columnconfigure(0, weight=1)
            ttk.Checkbutton(row, text="清理", variable=choice).grid(
                row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12)
            )
            ttk.Label(
                row,
                text="pip 官方缓存；通过 pip cache purge 清空，必要时可重新下载/构建",
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
            f"已定位 {len(visible)} 处 pip 缓存，共 {total}；默认建议清理约 {recommended}。"
        )


def open_pip_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _PipMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_pip_maintenance_dialog"]
