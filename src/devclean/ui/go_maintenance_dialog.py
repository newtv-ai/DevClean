"""Go cache maintenance UI: build cache local, module cache user intent."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.go_maintenance import (
    GoCacheCleanResult,
    GoCacheEntry,
    GoCacheKind,
    GoMaintenanceLane,
    GoStorageInventory,
    clean_go_cache,
    inventory_go_storage,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: GoStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[GoCacheCleanResult, ...]
    error: str | None = None


class _GoMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Go 缓存维护")
        self._window.geometry("960x640")
        self._window.minsize(820, 540)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Go 缓存…")
        self._inventory: GoStorageInventory | None = None
        self._choices: dict[GoCacheKind, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Go 缓存维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "DevClean 能本地确定 Go build cache 是可重建编译产物，所以不需要 AI。"
                "module cache 则是多个项目共享的已下载依赖源码：技术上 Go 可以安全清空，"
                "但离线环境或旧项目是否仍需要这些依赖属于你的使用意图，因此留给你决定。"
            ),
            wraplength=920,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "所有清理都由 Go 官方 `go clean` 执行。Build cache 达到 1 GiB 才默认勾选；"
                "module cache 无论多大都不会默认勾选。DevClean 会在执行前让同一个 Go"
                " 可执行文件再次确认 GOCACHE/GOMODCACHE 的目标路径。"
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
        self._status.set("正在统计 Go 缓存…")

        def work() -> None:
            try:
                inventory = inventory_go_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Go-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[GoCacheEntry] = []
        for entry in self._inventory.caches:
            choice = self._choices.get(entry.kind)
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("Go 缓存维护", "没有勾选需要清理的 Go 缓存。")
            return

        has_module = any(entry.kind is GoCacheKind.MODULE for entry in selected)
        if has_module and not messagebox.askyesno(
            "确认清理 Go module cache",
            "module cache 保存多个 Go 项目共享的已下载依赖源码。\n\n"
            "清空后，未来构建需要重新下载缺失模块；离线或私有依赖环境可能无法立即恢复。\n\n"
            "仍然清理你主动勾选的 module cache 吗？",
            icon=messagebox.WARNING,
        ):
            return

        selected.sort(key=lambda entry: entry.kind is GoCacheKind.MODULE)
        self._set_busy(True)
        self._status.set(f"正在通过 Go 官方命令清理 {len(selected)} 项…")

        def work(entries: tuple[GoCacheEntry, ...] = tuple(selected)) -> None:
            results: list[GoCacheCleanResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(clean_go_cache(entry.kind, entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-Go-cleanup", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"Go 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                text = f"Go 官方清理完成，共释放约 {_format_bytes(reclaimed)}"
            else:
                text = (
                    f"已完成 {len(outcome.results)} 项并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}"
                )
            self._status.set(f"{text}；正在重新统计…")
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: GoStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.caches if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            lane = (
                "确定可清理"
                if entry.lane is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
                else "你来决定"
            )
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[entry.kind] = choice
            title = f"{lane} · {_kind_label(entry.kind)} · {_format_bytes(entry.logical_bytes)}"
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

        total = _format_bytes(inventory.total_cache_bytes)
        deterministic = _format_bytes(inventory.deterministic_bytes)
        recommended = _format_bytes(inventory.recommended_bytes)
        self._status.set(
            f"Go 缓存共 {total}；本地确定可清理 {deterministic}；"
            f"默认建议清理约 {recommended}。"
        )


def open_go_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _GoMaintenanceDialog(parent).show()


def _kind_label(kind: GoCacheKind) -> str:
    return "build cache" if kind is GoCacheKind.BUILD else "module cache"


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_go_maintenance_dialog"]
