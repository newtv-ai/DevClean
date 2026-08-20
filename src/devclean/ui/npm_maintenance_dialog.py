"""npm cache maintenance UI backed by npm's current vendor commands."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.npm_maintenance import (
    NpmCacheVerifyResult,
    NpmContentCleanResult,
    NpmNpxEntry,
    NpmNpxRemoveResult,
    NpmStorageInventory,
    clean_npm_content_cache,
    inventory_npm_storage,
    remove_npm_npx_entry,
    verify_npm_content_cache,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: NpmStorageInventory


@dataclass(frozen=True, slots=True)
class _VerifyEvent:
    result: NpmCacheVerifyResult | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _CleanEvent:
    result: NpmContentCleanResult | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _NpxRemoveEvent:
    result: NpmNpxRemoveResult | None
    error: str | None = None


_Event = _InventoryEvent | _VerifyEvent | _CleanEvent | _NpxRemoveEvent | Exception


class _NpmMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("npm 缓存维护")
        self._window.geometry("1080x720")
        self._window.minsize(900, 600)
        self._events: queue.Queue[_Event] = queue.Queue()
        self._status = tk.StringVar(value="正在通过 npm 检查当前 cache…")
        self._inventory: NpmStorageInventory | None = None
        self._busy = False
        self._npx_by_iid: dict[str, NpmNpxEntry] = {}

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="npm 缓存维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "package cache、npx cache 和 TUF 状态不再由 DevClean 直接删目录。"
                "package cache 使用 npm cache verify/clean；npx 只删除 npm 自己列出的精确 entry。"
                "全局安装、.npmrc、package-lock 等仍受保护。"
            ),
            wraplength=1030,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        summary = ttk.LabelFrame(container, text="当前 npm cache", padding=10)
        summary.pack(fill=tk.X)
        self._root_label = ttk.Label(summary, text="cache 根目录：检查中…", wraplength=1000)
        self._root_label.pack(anchor=tk.W)
        self._size_label = ttk.Label(summary, text="", wraplength=1000)
        self._size_label.pack(anchor=tk.W, pady=(4, 0))

        actions = ttk.Frame(summary)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._verify_button = ttk.Button(
            actions,
            text="运行 npm cache verify",
            command=self._start_verify,
            state=tk.DISABLED,
        )
        self._verify_button.pack(side=tk.LEFT)
        self._clean_button = ttk.Button(
            actions,
            text="清空 package cache…",
            command=self._start_clean,
            state=tk.DISABLED,
        )
        self._clean_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            actions,
            text="verify 会做完整性检查和 npm 自己的垃圾回收；clean 会清空 _cacache，需要重新下载。",
            wraplength=680,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(12, 0))

        npx_frame = ttk.LabelFrame(container, text="npm exec / npx cache entries", padding=10)
        npx_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._tree = ttk.Treeview(
            npx_frame,
            columns=("key", "description", "size", "files"),
            show="headings",
            height=12,
            selectmode="browse",
        )
        self._tree.heading("key", text="npm vendor key")
        self._tree.heading("description", text="npm 说明")
        self._tree.heading("size", text="逻辑大小")
        self._tree.heading("files", text="文件数")
        self._tree.column("key", width=230, anchor=tk.W)
        self._tree.column("description", width=480, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("files", width=80, anchor=tk.E)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._selection_changed)

        npx_actions = ttk.Frame(npx_frame)
        npx_actions.pack(fill=tk.X, pady=(8, 0))
        self._remove_npx_button = ttk.Button(
            npx_actions,
            text="删除所选精确 npx entry…",
            command=self._start_npx_remove,
            state=tk.DISABLED,
        )
        self._remove_npx_button.pack(side=tk.LEFT)
        ttk.Label(
            npx_actions,
            text=(
                "DevClean 总是把 npm cache npx ls 返回的完整 key 交回 npm；"
                "先用 --dry-run 验证精确路径，再执行，不使用缩写 key，也不使用 --force 清空全部。"
            ),
            wraplength=760,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(
            container,
            text=(
                "_tuf 仅显示占用，不提供原始删除。所有大小均为逻辑文件统计，"
                "不保证与 Windows 物理可回收空间一一对应。"
            ),
            wraplength=1030,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=1030,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
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
        normal = tk.DISABLED if busy else tk.NORMAL
        self._refresh_button.configure(state=normal)
        available = self._inventory is not None and not busy
        self._verify_button.configure(state=tk.NORMAL if available else tk.DISABLED)
        self._clean_button.configure(state=tk.NORMAL if available else tk.DISABLED)
        self._selection_changed(None)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在通过 npm config/cache 命令确认当前 cache 并统计逻辑大小…")

        def work() -> None:
            try:
                self._events.put(_InventoryEvent(inventory_npm_storage()))
            except Exception as error:
                self._events.put(error)

        threading.Thread(target=work, name="DevClean-npm-inventory", daemon=True).start()

    def _start_verify(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        self._set_busy(True)
        self._status.set("正在运行 npm cache verify；npm 将自行校验并回收无引用内容…")

        def work(reviewed: NpmStorageInventory = inventory) -> None:
            try:
                result = verify_npm_content_cache(reviewed)
            except Exception as error:
                self._events.put(_VerifyEvent(None, str(error)))
            else:
                self._events.put(_VerifyEvent(result))

        threading.Thread(target=work, name="DevClean-npm-verify", daemon=True).start()

    def _start_clean(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        if not messagebox.askyesno(
            "清空 npm package cache",
            (
                f"将通过 `npm cache clean --force` 清空：\n{inventory.content_cache.path}\n\n"
                f"当前逻辑大小约 {_format_bytes(inventory.content_cache.logical_bytes)}，"
                f"npm 列出 {len(inventory.content_keys)} 个 cache key。\n\n"
                "这不会卸载已安装的包，但之后安装依赖可能需要重新下载。继续吗？"
            ),
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set("正在重新验证审核状态，然后通过 npm 官方 clean 清空 package cache…")

        def work(reviewed: NpmStorageInventory = inventory) -> None:
            try:
                result = clean_npm_content_cache(reviewed)
            except Exception as error:
                self._events.put(_CleanEvent(None, str(error)))
            else:
                self._events.put(_CleanEvent(result))

        threading.Thread(target=work, name="DevClean-npm-clean", daemon=True).start()

    def _start_npx_remove(self) -> None:
        inventory = self._inventory
        selection = self._tree.selection()
        if self._busy or inventory is None or not selection:
            return
        entry = self._npx_by_iid.get(selection[0])
        if entry is None:
            return
        if not messagebox.askyesno(
            "删除 npx cache entry",
            (
                f"npm vendor key：{entry.key}\n"
                f"说明：{entry.description or '(unknown)'}\n"
                f"路径：{entry.path}\n"
                f"逻辑大小：{_format_bytes(entry.logical_bytes)}\n\n"
                "DevClean 会先运行 npm 的 --dry-run 并核对精确路径，然后只删除这个 key。继续吗？"
            ),
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set(f"正在重新验证并删除精确 npx entry：{entry.key}…")

        def work(
            reviewed: NpmStorageInventory = inventory,
            expected: NpmNpxEntry = entry,
        ) -> None:
            try:
                result = remove_npm_npx_entry(reviewed, expected)
            except Exception as error:
                self._events.put(_NpxRemoveEvent(None, str(error)))
            else:
                self._events.put(_NpxRemoveEvent(result))

        threading.Thread(target=work, name="DevClean-npm-npx-remove", daemon=True).start()

    def _selection_changed(self, _event: tk.Event[tk.Misc] | None) -> None:
        can_remove = not self._busy and self._inventory is not None and bool(self._tree.selection())
        self._remove_npx_button.configure(state=tk.NORMAL if can_remove else tk.DISABLED)

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"npm 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        elif isinstance(outcome, _VerifyEvent):
            if outcome.error is not None or outcome.result is None:
                self._status.set(f"npm cache verify 失败：{outcome.error or 'unknown error'}")
                self._set_busy(False)
            else:
                self._status.set(
                    "npm cache verify 完成：逻辑统计约释放 "
                    f"{_format_bytes(outcome.result.reclaimed_bytes)}，正在重新统计…"
                )
                self._finish_and_refresh()
        elif isinstance(outcome, _CleanEvent):
            if outcome.error is not None or outcome.result is None:
                self._status.set(f"npm package cache 清空失败：{outcome.error or 'unknown error'}")
                self._set_busy(False)
            else:
                self._status.set(
                    f"npm package cache 已清空 {outcome.result.removed_keys} 个 vendor key，"
                    f"逻辑统计约释放 {_format_bytes(outcome.result.reclaimed_bytes)}；正在重新统计…"
                )
                self._finish_and_refresh()
        else:
            if outcome.error is not None or outcome.result is None:
                self._status.set(f"npx entry 删除失败：{outcome.error or 'unknown error'}")
                self._set_busy(False)
            else:
                self._status.set(
                    f"已由 npm 删除 npx key {outcome.result.key}，逻辑统计约释放 "
                    f"{_format_bytes(outcome.result.reclaimed_bytes)}；正在重新统计…"
                )
                self._finish_and_refresh()
        self._window.after(100, self._poll)

    def _finish_and_refresh(self) -> None:
        self._busy = False
        self._start_inventory()

    def _render(self, inventory: NpmStorageInventory) -> None:
        self._root_label.configure(text=f"cache 根目录：{inventory.cache_root}")
        self._size_label.configure(
            text=(
                f"package (_cacache)：{_format_bytes(inventory.content_cache.logical_bytes)} / "
                f"{len(inventory.content_keys)} keys；"
                f"npx (_npx)：{_format_bytes(inventory.npx_cache.logical_bytes)} / "
                f"{len(inventory.npx_entries)} entries；"
                f"TUF (_tuf，保护)：{_format_bytes(inventory.tuf_cache.logical_bytes)}"
            )
        )
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._npx_by_iid.clear()
        for index, entry in enumerate(inventory.npx_entries):
            iid = str(index)
            self._npx_by_iid[iid] = entry
            self._tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    entry.key,
                    entry.description or "(unknown)",
                    _format_bytes(entry.logical_bytes),
                    str(entry.file_count),
                ),
            )
        warning = "；".join(inventory.warnings)
        suffix = f" 警告：{warning}" if warning else ""
        self._status.set(
            f"已由 npm 确认当前 cache；总逻辑统计约 {_format_bytes(inventory.total_cache_bytes)}。"
            + suffix
        )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024.0 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{value:,} B"


def open_npm_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _NpmMaintenanceDialog(parent).show()


__all__ = ["open_npm_maintenance_dialog"]
