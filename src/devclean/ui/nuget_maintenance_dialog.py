"""NuGet maintenance UI: deterministic caches first, user intent second."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.nuget_maintenance import (
    NuGetClearResult,
    NuGetLocalEntry,
    NuGetLocalKind,
    NuGetMaintenanceLane,
    NuGetStorageInventory,
    clear_nuget_local,
    inventory_nuget_storage,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: NuGetStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[NuGetClearResult, ...]
    error: str | None = None


class _NuGetMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("NuGet 缓存维护")
        self._window.geometry("980x650")
        self._window.minsize(820, 560)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 NuGet 本地存储…")
        self._inventory: NuGetStorageInventory | None = None
        self._choices: dict[NuGetLocalKind, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="NuGet 本地存储",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "DevClean 本地就能确定 HTTP / 临时 / 插件缓存属于 NuGet 官方可清理资源，"
                "不需要把它们交给 AI。global-packages 则可能被项目直接使用，所以只告诉你"
                "占用并由你决定。所有清理都调用 dotnet nuget locals，不直接删除内部文件。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        legend = ttk.Frame(container)
        legend.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(legend, text="确定可清理：厂商定义缓存，达到收益阈值时默认勾选").pack(
            side=tk.LEFT
        )
        ttk.Label(legend, text="你来决定：删除安全，但是否还需要取决于你的项目").pack(side=tk.RIGHT)

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "注意：清理 global-packages 后，使用 PackageReference 的项目需要重新 restore；"
                "离线环境尤其不建议随手清空。HTTP / 临时 / 插件缓存清理也会先确认 NuGet、"
                ".NET、MSBuild 或 Visual Studio 没有正在使用相关文件。"
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
        self._status.set("正在统计 NuGet 本地存储…")

        def work() -> None:
            try:
                inventory = inventory_nuget_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-NuGet-inventory", daemon=True).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[NuGetLocalEntry] = []
        for entry in self._inventory.locals:
            choice = self._choices.get(entry.kind)
            if entry.exists and choice is not None and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo("NuGet 缓存维护", "没有勾选需要清理的 NuGet 存储。")
            return
        has_global_packages = any(
            entry.kind is NuGetLocalKind.GLOBAL_PACKAGES for entry in selected
        )
        if has_global_packages and not messagebox.askyesno(
            "确认清理 global-packages",
            "global-packages 是项目可能直接使用的已还原依赖。\n\n"
            "清空后需要重新 restore 才能继续使用这些包；离线项目可能无法立即恢复。\n\n"
            "仍然清理你主动勾选的 global-packages 吗？",
            icon=messagebox.WARNING,
        ):
            return

        selected.sort(key=lambda entry: entry.kind is NuGetLocalKind.GLOBAL_PACKAGES)
        self._set_busy(True)
        self._status.set(f"正在通过 NuGet 官方命令清理 {len(selected)} 项…")

        def work(entries: tuple[NuGetLocalEntry, ...] = tuple(selected)) -> None:
            results: list[NuGetClearResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(clear_nuget_local(entry.kind, entry.path))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(target=work, name="DevClean-NuGet-cleanup", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"NuGet 操作失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                self._status.set(
                    f"NuGet 官方清理完成，共释放约 {_format_bytes(reclaimed)}；正在重新统计…"
                )
            else:
                self._status.set(
                    f"已完成 {len(outcome.results)} 项并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}；正在重新统计…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: NuGetStorageInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()

        visible = [entry for entry in inventory.locals if entry.exists]
        visible.sort(key=lambda entry: entry.logical_bytes, reverse=True)
        for row_index, entry in enumerate(visible):
            lane_text = (
                "确定可清理"
                if entry.lane is NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE
                else "你来决定"
            )
            choice = tk.BooleanVar(value=entry.recommended)
            self._choices[entry.kind] = choice
            title = (
                f"{lane_text} · {_kind_label(entry.kind)} · {_format_bytes(entry.logical_bytes)}"
            )
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

        safe_count = sum(
            entry.lane is NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE for entry in visible
        )
        user_count = sum(entry.lane is NuGetMaintenanceLane.USER_REVIEW for entry in visible)
        total_text = _format_bytes(inventory.total_local_bytes)
        self._status.set(
            f"已定位 {len(visible)} 处 NuGet 存储，共 {total_text}；"
            f"其中 {safe_count} 类可本地确定，{user_count} 类留给你决定。"
        )


def open_nuget_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _NuGetMaintenanceDialog(parent).show()


def _kind_label(kind: NuGetLocalKind) -> str:
    return {
        NuGetLocalKind.GLOBAL_PACKAGES: "全局包依赖",
        NuGetLocalKind.HTTP_CACHE: "HTTP 缓存",
        NuGetLocalKind.TEMP: "临时缓存",
        NuGetLocalKind.PLUGINS_CACHE: "插件缓存",
    }[kind]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_nuget_maintenance_dialog"]
