"""Unity Package Manager global-cache inventory and legacy cleanup UI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from devclean.core.unity_upm_maintenance import (
    UnityUpmInventory,
    UnityUpmLane,
    UnityUpmLegacyCleanResult,
    UnityUpmRootOrigin,
    UnityUpmStorageEntry,
    UnityUpmStorageKind,
    delete_unity_upm_legacy_packages,
    inventory_unity_upm_storage,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: UnityUpmInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[UnityUpmLegacyCleanResult, ...]
    error: str | None = None


class _UnityUpmMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Unity UPM 全局缓存维护")
        self._window.geometry("1080x720")
        self._window.minsize(900, 590)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="正在统计 Unity Package Manager 全局缓存…")
        self._inventory: UnityUpmInventory | None = None
        self._choices: dict[str, tk.BooleanVar] = {}
        self._entries: dict[str, UnityUpmStorageEntry] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Unity Package Manager 全局缓存",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Unity 6 的 UPM 缓存不能当成一个普通目录来清空。当前 registry `db` "
                "由 Package Manager 自己按大小上限和 LRU 规则回收；Git LFS 缓存可以减少"
                "重复下载；只有旧版 `packages` 子目录有官方明确的条件性删除说明。"
            ),
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        summary = ttk.LabelFrame(container, text="当前有效配置", padding=8)
        summary.pack(fill=tk.X, pady=(0, 8))
        self._summary_label = ttk.Label(
            summary,
            text="尚未完成统计。",
            wraplength=1020,
            justify=tk.LEFT,
        )
        self._summary_label.pack(anchor=tk.W)

        legend = ttk.Frame(container)
        legend.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(legend, text="Unity 自管：只统计，不与厂商 LRU/缓存策略竞争").pack(
            side=tk.LEFT
        )
        ttk.Label(legend, text="你来决定：只有旧版 packages，可手动选择").pack(
            side=tk.RIGHT
        )

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "安全边界：DevClean 不会删除整个 UPM 根，不会 raw-delete 当前 `db`，也不会"
                "清理 Git LFS 缓存。删除旧版 `packages` 前会重新解析当前 UPM 配置、确认精确"
                "缓存根、确认 Unity/Hub/Package Manager 已关闭，并使用 handle-bound 精确目录删除。"
            ),
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=1040,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="删除已选择的旧版 packages…",
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
        eligible = (
            not busy
            and self._inventory is not None
            and any(entry.deletable for entry in self._inventory.entries)
        )
        self._clean_button.configure(state=tk.NORMAL if eligible else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在解析 UPM 用户配置/环境覆盖并统计各个语义独立的缓存位置…")

        def work() -> None:
            try:
                inventory = inventory_unity_upm_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Unity-UPM-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        if self._busy or self._inventory is None:
            return
        selected: list[UnityUpmStorageEntry] = []
        for key, choice in self._choices.items():
            entry = self._entries.get(key)
            if entry is not None and entry.deletable and choice.get():
                selected.append(entry)
        if not selected:
            messagebox.showinfo(
                "Unity UPM 全局缓存维护",
                "没有选择要删除的旧版 packages 目录。",
                parent=self._window,
            )
            return

        total = sum(entry.logical_bytes for entry in selected)
        if not messagebox.askyesno(
            "确认删除旧版 UPM packages",
            (
                f"将删除 {len(selected)} 个旧版 UPM `packages` 目录，约 "
                f"{_format_bytes(total)}。\n\n"
                "Unity 6 已不再使用这些目录，但 Unity 官方的安全条件是：你已经不再维护需要"
                "旧版 Editor（例如 Unity 2023.2）处理的相关项目。\n\n"
                "如果你仍需要旧版 Unity 项目，请取消。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return

        self._set_busy(True)
        self._status.set(f"正在重新验证配置并精确删除 {len(selected)} 个旧版 packages…")

        def work(entries: tuple[UnityUpmStorageEntry, ...] = tuple(selected)) -> None:
            results: list[UnityUpmLegacyCleanResult] = []
            error_text: str | None = None
            for entry in entries:
                if entry.cache_root is None:
                    error_text = f"旧版 packages 缺少可验证缓存根: {entry.path}"
                    break
                try:
                    results.append(delete_unity_upm_legacy_packages(entry.cache_root))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-Unity-UPM-legacy-cleanup",
            daemon=True,
        ).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            outcome = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(outcome, Exception):
            self._status.set(f"Unity UPM 统计失败：{outcome}")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(result.reclaimed_bytes for result in outcome.results)
            if outcome.error is None:
                self._status.set(
                    f"已删除 {len(outcome.results)} 个旧版 packages，释放约 "
                    f"{_format_bytes(reclaimed)}；正在重新统计…"
                )
            else:
                self._status.set(
                    f"已完成 {len(outcome.results)} 项并释放约 {_format_bytes(reclaimed)}，"
                    f"随后停止：{outcome.error}；正在重新统计…"
                )
            self._busy = False
            self._start_inventory()
        self._window.after(100, self._poll)

    def _render(self, inventory: UnityUpmInventory) -> None:
        for widget in self._rows.winfo_children():
            widget.destroy()
        self._choices.clear()
        self._entries.clear()

        config_text = (
            str(inventory.user_config_path)
            if inventory.user_config_path is not None
            else "未解析到用户配置文件位置"
        )
        self._summary_label.configure(
            text=(
                f"有效全局缓存根: {inventory.active_root}\n"
                f"有效 registry db: {inventory.active_db}\n"
                f"db 最大值: {_format_bytes(inventory.db_max_bytes)} "
                f"({_origin_label(inventory.db_max_source)})\n"
                f"UPM 用户配置: {config_text}"
            )
        )

        visible = [entry for entry in inventory.entries if entry.exists]
        for index, entry in enumerate(visible):
            lane = _lane_label(entry.lane)
            active = " · 当前有效" if entry.active else ""
            title = (
                f"{lane}{active} · {_kind_label(entry.kind)} · "
                f"{_format_bytes(entry.logical_bytes)}"
            )
            row = ttk.LabelFrame(self._rows, text=title, padding=8)
            row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
            self._rows.columnconfigure(0, weight=1)

            key = f"entry-{index}"
            self._entries[key] = entry
            if entry.deletable:
                choice = tk.BooleanVar(value=False)
                self._choices[key] = choice
                ttk.Checkbutton(row, text="删除", variable=choice).grid(
                    row=0,
                    column=0,
                    rowspan=3,
                    sticky="nw",
                    padx=(0, 12),
                )
            else:
                ttk.Label(row, text="不删除").grid(
                    row=0,
                    column=0,
                    rowspan=3,
                    sticky="nw",
                    padx=(0, 12),
                )

            ttk.Label(row, text=entry.reason, wraplength=800, justify=tk.LEFT).grid(
                row=0,
                column=1,
                sticky="w",
            )
            ttk.Label(row, text=str(entry.path), wraplength=800, justify=tk.LEFT).grid(
                row=1,
                column=1,
                sticky="w",
                pady=(3, 0),
            )
            if entry.cache_root is not None:
                ttk.Label(
                    row,
                    text=f"缓存根: {entry.cache_root}",
                    wraplength=800,
                    justify=tk.LEFT,
                ).grid(row=2, column=1, sticky="w", pady=(3, 0))
            row.columnconfigure(1, weight=1)

        legacy_count = sum(
            entry.deletable and entry.kind is UnityUpmStorageKind.LEGACY_PACKAGES
            for entry in visible
        )
        self._status.set(
            f"已定位 {len(visible)} 个 UPM 存储项，共约 "
            f"{_format_bytes(inventory.total_visible_bytes)}；"
            f"可由你决定删除的旧版 packages 有 {legacy_count} 个。当前 db 不会被 DevClean 清理。"
        )


def open_unity_upm_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _UnityUpmMaintenanceDialog(parent).show()


def _lane_label(lane: UnityUpmLane) -> str:
    return {
        UnityUpmLane.UNITY_MANAGED: "Unity 自管",
        UnityUpmLane.USER_REVIEW: "你来决定",
        UnityUpmLane.REPORT_ONLY: "仅报告",
    }[lane]


def _kind_label(kind: UnityUpmStorageKind) -> str:
    return {
        UnityUpmStorageKind.DB: "Registry db",
        UnityUpmStorageKind.LEGACY_PACKAGES: "旧版 packages",
        UnityUpmStorageKind.GIT_LFS: "Git LFS 缓存",
    }[kind]


def _origin_label(origin: UnityUpmRootOrigin) -> str:
    return {
        UnityUpmRootOrigin.DEFAULT: "默认值",
        UnityUpmRootOrigin.USER_CONFIG: "用户配置",
        UnityUpmRootOrigin.ENVIRONMENT: "环境变量",
    }[origin]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_unity_upm_maintenance_dialog"]
