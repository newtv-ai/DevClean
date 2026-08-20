"""Project-aware Unity Library cleanup dialog."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.unity_project_maintenance import (
    UnityLibraryCleanResult,
    UnityProjectLibraryInventory,
    delete_unity_project_library,
    inspect_unity_project,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: UnityProjectLibraryInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: UnityLibraryCleanResult | None
    error: str | None = None


class _UnityProjectMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Unity 项目 Library 维护")
        self._window.geometry("940x560")
        self._window.minsize(800, 500)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="请选择 Unity 项目根目录进行精确检查.")
        self._project = tk.StringVar(value="")
        self._inventory: UnityProjectLibraryInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Unity 项目 Library 维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Unity 官方明确说明: 项目关闭时 Library 可以由 Assets 和 ProjectSettings "
                "重新生成. 但完整删除会触发重新导入和重建, 所以这是用户决定, 不是默认清理, "
                "也不需要 AI."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        picker = ttk.Frame(container)
        picker.pack(fill=tk.X)
        ttk.Entry(picker, textvariable=self._project, state="readonly").pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
        )
        self._choose_button = ttk.Button(
            picker,
            text="选择 Unity 项目...",
            command=self._choose_project,
        )
        self._choose_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="项目确认结果", padding=10)
        details.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="尚未检查项目.",
            wraplength=860,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        ttk.Label(
            container,
            text=(
                "安全边界: 只接受包含 Assets 和 ProjectSettings/ProjectVersion.txt 的项目根目录; "
                "只允许删除该根目录的直接子目录 Library; 链接/junction 会被拒绝; "
                "任何 Unity Editor 正在运行时都拒绝执行."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            text=(
                "本工具不碰 Assets、Packages、ProjectSettings、UserSettings, 也不把 UPM 全局缓存、"
                "Asset Store 缓存或 GI Cache 混进这条规则."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="删除项目 Library...",
            command=self._start_cleanup,
            state=tk.DISABLED,
        )
        self._clean_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._choose_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.exists
        )
        self._clean_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _choose_project(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Unity 项目根目录",
            mustexist=True,
        )
        if not selected:
            return
        self._project.set(selected)
        self._start_inventory(Path(selected))

    def _start_inventory(self, project: Path) -> None:
        self._inventory = None
        self._set_busy(True)
        self._status.set("正在验证 Unity 项目边界并统计 Library...")

        def work() -> None:
            try:
                inventory = inspect_unity_project(project)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Unity-project-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None or not inventory.exists:
            return
        confirmed = messagebox.askyesno(
            "删除 Unity 项目 Library",
            (
                f"将永久删除:\n{inventory.library}\n\n"
                f"当前占用约 {_format_bytes(inventory.logical_bytes)}.\n"
                "Unity 下次打开项目时会重新导入/重建这些数据, 大项目可能耗时很久. "
                "DevClean 会先确认所有 Unity Editor 都已关闭.\n\n"
                "确定继续吗?"
            ),
            parent=self._window,
        )
        if not confirmed:
            return
        self._set_busy(True)
        self._status.set("正在重新验证项目边界并删除精确的 Library 目录...")
        project = inventory.project_root

        def work() -> None:
            try:
                result = delete_unity_project_library(project)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Unity-library-cleanup",
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
            self._inventory = None
            self._details.configure(text=f"无法确认 Unity 项目: {outcome}")
            self._status.set("未执行任何删除.")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"Unity Library 删除失败: {outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                reclaimed = _format_bytes(outcome.result.reclaimed_bytes)
                self._status.set(f"Unity Library 删除完成, 释放约 {reclaimed}.")
                self._start_inventory(outcome.result.project_root)
        self._window.after(100, self._poll)

    def _render(self, inventory: UnityProjectLibraryInventory) -> None:
        self._project.set(str(inventory.project_root))
        if not inventory.exists:
            decision = "当前项目没有 Library, 无需执行."
        elif inventory.worth_reviewing:
            decision = "空间占用较大, 值得由用户决定是否用重新导入时间换取磁盘空间."
        else:
            decision = "当前占用不大, 默认不建议为了省空间触发完整重新导入."
        self._details.configure(
            text=(
                f"Unity 版本: {inventory.editor_version}\n"
                f"项目根目录: {inventory.project_root}\n"
                f"精确目标: {inventory.library}\n"
                f"Library 当前占用: {_format_bytes(inventory.logical_bytes)}\n\n"
                f"判定: {decision}\n"
                "执行级别: USER_REVIEW, 永不自动选择, 不调用 AI."
            )
        )
        self._status.set("检查完成. 只有精确项目 Library 获得用户确认后的删除权限.")


def open_unity_project_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _UnityProjectMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_unity_project_maintenance_dialog"]
