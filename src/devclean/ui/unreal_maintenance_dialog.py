"""Unreal Engine DDC maintenance UI."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from devclean.core.unreal_maintenance import (
    UnrealDDCCleanupResult,
    UnrealStorageInventory,
    inventory_unreal_storage,
    run_unreal_ddc_cleanup,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: UnrealStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: UnrealDDCCleanupResult | None
    error: str | None = None


class _UnrealMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Unreal DDC 维护")
        self._window.geometry("980x680")
        self._window.minsize(840, 560)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在定位 Unreal Engine 与 DDC...")
        self._selected_editor = tk.StringVar(value="")
        self._inventory: UnrealStorageInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Unreal Engine Derived Data Cache 维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "DDC 是可重新生成的派生数据. 但 UE 5.4+ 的 Zen 数据目录还可能保存 cooked output, "
                "因此 DevClean 不直接删除 DerivedDataCache 或 Zen/Data. 清理由 Unreal 自己的 "
                "DDCCleanup commandlet 完成, 让实际 cache backend 决定哪些 stale 数据可回收."
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        engine_box = ttk.LabelFrame(container, text="用于执行维护的 Unreal Engine", padding=8)
        engine_box.pack(fill=tk.X)
        self._engine_rows = ttk.Frame(engine_box)
        self._engine_rows.pack(fill=tk.X)

        storage_box = ttk.LabelFrame(container, text="已知本地 DDC / Zen 占用", padding=8)
        storage_box.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._storage_rows = ttk.Frame(storage_box)
        self._storage_rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "显示的大小是 DevClean 能定位到的相关存储总量, 不是承诺可回收量. Zen/Data 可能混有"
                " cooked output. 实际释放空间只在 Unreal 官方维护完成后按前后差值统计."
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
        self._cleanup_button = ttk.Button(
            footer,
            text="运行 Unreal DDC 官方维护",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._cleanup_button.pack(side=tk.RIGHT, padx=(8, 0))
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
        available = (
            not busy
            and self._inventory is not None
            and bool(self._inventory.engines)
            and bool(self._selected_editor.get())
        )
        self._cleanup_button.configure(state=tk.NORMAL if available else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在统计 Unreal DDC / Zen storage...")

        def work() -> None:
            try:
                inventory = inventory_unreal_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Unreal-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected = self._selected_editor.get()
        if not selected:
            messagebox.showinfo("Unreal DDC 维护", "没有选择可用的 Unreal Engine.")
            return
        self._set_busy(True)
        self._status.set("正在运行 Unreal DDCCleanup commandlet...")

        def work() -> None:
            try:
                result = run_unreal_ddc_cleanup(Path(selected))
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(target=work, name="DevClean-Unreal-DDC-cleanup", daemon=True).start()

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
            self._status.set(f"Unreal DDC 统计失败: {outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"Unreal DDC 维护失败: {outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                reclaimed = _format_bytes(outcome.result.observed_reclaimed_bytes)
                self._status.set(
                    f"Unreal DDC 官方维护完成. 已知存储观察到释放约 {reclaimed}. 正在重新统计..."
                )
                self._busy = False
                self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: UnrealStorageInventory) -> None:
        for widget in self._engine_rows.winfo_children():
            widget.destroy()
        for widget in self._storage_rows.winfo_children():
            widget.destroy()

        current = self._selected_editor.get()
        engine_values = {str(engine.editor_cmd) for engine in inventory.engines}
        if current not in engine_values:
            self._selected_editor.set(
                str(inventory.engines[-1].editor_cmd) if inventory.engines else ""
            )
        for index, engine in enumerate(inventory.engines):
            ttk.Radiobutton(
                self._engine_rows,
                text=str(engine.editor_cmd),
                value=str(engine.editor_cmd),
                variable=self._selected_editor,
                command=lambda: self._set_busy(False),
            ).grid(row=index, column=0, sticky="w", pady=(0, 4))
        self._engine_rows.columnconfigure(0, weight=1)

        visible = sorted(
            (entry for entry in inventory.stores if entry.exists),
            key=lambda entry: entry.logical_bytes,
            reverse=True,
        )
        for index, entry in enumerate(visible):
            frame = ttk.Frame(self._storage_rows)
            frame.grid(row=index, column=0, sticky="ew", pady=(0, 8))
            ttk.Label(
                frame,
                text=f"{entry.kind.value} - {_format_bytes(entry.logical_bytes)}",
                font=("Segoe UI", 10, "bold"),
            ).grid(row=0, column=0, sticky="w")
            ttk.Label(frame, text=str(entry.path), wraplength=850, justify=tk.LEFT).grid(
                row=1, column=0, sticky="w"
            )
            ttk.Label(frame, text=entry.note, wraplength=850, justify=tk.LEFT).grid(
                row=2, column=0, sticky="w"
            )
            frame.columnconfigure(0, weight=1)
        self._storage_rows.columnconfigure(0, weight=1)

        total = _format_bytes(inventory.total_known_bytes)
        if not inventory.engines:
            self._status.set(
                f"已知 Unreal DDC / Zen 占用约 {total}, 但未找到 UnrealEditor-Cmd, 暂不执行维护."
            )
        else:
            recommendation = "建议运行维护" if inventory.recommended else "当前不默认建议运行"
            engine_count = len(inventory.engines)
            self._status.set(
                f"找到 {engine_count} 个 Unreal Engine, 已知相关存储约 {total}. {recommendation}."
            )


def open_unreal_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _UnrealMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_unreal_maintenance_dialog"]
