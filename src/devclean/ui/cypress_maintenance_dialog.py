"""Cypress binary-cache maintenance UI backed by exact vendor prune."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from devclean.core.cypress_maintenance import (
    CypressPruneResult,
    CypressStorageInventory,
    inventory_cypress_storage,
    prune_cypress_binary_cache,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: CypressStorageInventory


@dataclass(frozen=True, slots=True)
class _PruneEvent:
    result: CypressPruneResult


class _CypressMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Cypress binary cache 维护")
        self._window.geometry("980x650")
        self._window.minsize(840, 560)
        self._events: queue.Queue[_InventoryEvent | _PruneEvent | Exception] = queue.Queue()
        self._inventory: CypressStorageInventory | None = None
        self._busy = False
        self._cli = tk.StringVar(value="")
        self._status = tk.StringVar(value="请选择 Cypress CLI，或留空从 PATH 自动查找。")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Cypress binary cache",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Cypress 的下载 binary cache 会被多个项目共享。DevClean 不按年龄猜测旧版本是否"
                "没用，也不直接删除 Cache 目录；这里只包装当前 Cypress 官方 `cache prune`。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        selector = ttk.Frame(container)
        selector.pack(fill=tk.X)
        ttk.Label(selector, text="Cypress CLI:").pack(side=tk.LEFT)
        self._cli_entry = ttk.Entry(selector, textvariable=self._cli)
        self._cli_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self._browse_button = ttk.Button(selector, text="选择…", command=self._browse)
        self._browse_button.pack(side=tk.LEFT)
        self._refresh_button = ttk.Button(selector, text="检查", command=self._start_inventory)
        self._refresh_button.pack(side=tk.LEFT, padx=(8, 0))

        self._summary = ttk.LabelFrame(container, text="审核结果", padding=10)
        self._summary.pack(fill=tk.X, pady=(12, 0))
        self._summary_text = ttk.Label(
            self._summary,
            text="尚未检查。",
            wraplength=910,
            justify=tk.LEFT,
        )
        self._summary_text.pack(anchor=tk.W)

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        ttk.Label(
            container,
            text=(
                "重要：`cache prune` 保留的是你所选 CLI package 自己的版本，不代表其他项目都"
                "不再使用旧版本；旧项目之后可能重新下载。当前 Cypress 源码还会顺带清理 dead "
                "session records，因此这里始终要求用户确认。`cache clear` 不提供：源码会移除整个"
                "cache root，包括 bundles / sessions 等相邻状态。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._prune_button = ttk.Button(
            footer,
            text="审核并 prune 旧 binary cache…",
            command=self._start_prune,
            state=tk.DISABLED,
        )
        self._prune_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _browse(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self._window,
            title="选择已安装的 Cypress CLI",
            filetypes=(
                ("Cypress command files", "*.cmd *.exe *.bat"),
                ("All files", "*.*"),
            ),
        )
        if selected:
            self._cli.set(selected)
            self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self._browse_button.configure(state=state)
        self._refresh_button.configure(state=state)
        self._cli_entry.configure(state=state)
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.prune_supported
            and bool(self._inventory.prune_candidates)
        )
        self._prune_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _selected_cli(self) -> str | None:
        value = self._cli.get().strip()
        return value or None

    def _start_inventory(self) -> None:
        if self._busy:
            return
        cli_path = self._selected_cli()
        self._set_busy(True)
        self._status.set("正在通过 Cypress CLI 确认 package version 和 exact cache path…")

        def work() -> None:
            try:
                inventory = inventory_cypress_storage(cli_path)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-cypress-inventory", daemon=True).start()

    def _start_prune(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None or not inventory.prune_candidates:
            return
        candidates = ", ".join(item.version for item in inventory.prune_candidates)
        current_note = (
            f"当前 CLI package version {inventory.package_version} 的 cache 会保留。"
            if any(item.current_package_version for item in inventory.versions)
            else (
                f"当前 CLI package version {inventory.package_version} 尚未出现在 cache 中；"
                "这次 prune 会删除当前看到的全部旧 binary 版本。"
            )
        )
        text = (
            f"将通过精确 Cypress CLI 删除以下旧 binary cache：\n\n{candidates}\n\n"
            f"逻辑大小约 {_format_bytes(inventory.prune_candidate_bytes)}。\n\n"
            f"{current_note}\n\n"
            "其他项目如果仍使用这些版本，之后需要重新下载。Cypress prune 还会清理 dead "
            "session records，但不会使用 DevClean 自己的递归删除。继续吗？"
        )
        if not messagebox.askyesno("确认 Cypress cache prune", text, parent=self._window):
            return

        reviewed = inventory
        cli_path = str(inventory.cli_tool.path)
        self._set_busy(True)
        self._status.set("正在重新验证 exact cache scope，然后运行 Cypress 官方 prune…")

        def work() -> None:
            try:
                result = prune_cypress_binary_cache(reviewed, cli_path)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_PruneEvent(result))

        threading.Thread(target=work, name="DevClean-cypress-prune", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"Cypress 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._cli.set(str(outcome.inventory.cli_tool.path))
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            removed = ", ".join(outcome.result.removed_versions)
            self._status.set(
                "Cypress 官方 prune 已完成："
                f"{removed}；binary 逻辑大小减少约 "
                f"{_format_bytes(outcome.result.logical_reclaimed_bytes)}。正在重新检查…"
            )
            self._busy = False
            self._start_inventory()

        self._window.after(100, self._poll)

    def _render(self, inventory: CypressStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()

        if not inventory.versions:
            ttk.Label(
                self._rows,
                text="没有发现可识别的 semver Cypress binary cache。",
            ).pack(anchor=tk.W)
        else:
            for item in inventory.versions:
                role = (
                    "当前 CLI 版本 · 保留" if item.current_package_version else "旧版本 · 你来决定"
                )
                frame = ttk.LabelFrame(
                    self._rows,
                    text=f"{item.version} · {role} · {_format_bytes(item.logical_bytes)}",
                    padding=8,
                )
                frame.pack(fill=tk.X, pady=(0, 6))
                ttk.Label(
                    frame,
                    text=f"{item.path} · {item.file_count:,} 个文件",
                    wraplength=900,
                    justify=tk.LEFT,
                ).pack(anchor=tk.W)

        details = (
            f"CLI package: {inventory.package_version} | cache: {inventory.cache_root} | "
            f"binary cache: {_format_bytes(inventory.binary_bytes)}"
        )
        if inventory.external_entries:
            details += " | 保留相邻状态: " + ", ".join(inventory.external_entries)
        if inventory.unknown_entries:
            details += " | 未知顶层对象（prune 已禁用）: " + ", ".join(inventory.unknown_entries)
        self._summary_text.configure(text=details)

        if inventory.unknown_entries:
            self._status.set(
                "检测到当前审计源码未定义的 cache 顶层对象；为避免未来 Cypress 语义扩大，"
                "DevClean fail closed，不运行 prune。"
            )
        elif not inventory.prune_candidates:
            self._status.set("没有其他 binary cache 版本需要 prune。")
        else:
            self._status.set(
                f"可审核 prune {len(inventory.prune_candidates)} 个旧版本，逻辑大小约 "
                f"{_format_bytes(inventory.prune_candidate_bytes)}；不会默认执行。"
            )


def open_cypress_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _CypressMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_cypress_maintenance_dialog"]
