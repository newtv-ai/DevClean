"""Project-aware Bazel cleanup dialog."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.bazel_maintenance import (
    BazelCleanMode,
    BazelCleanResult,
    BazelWorkspaceInventory,
    clean_bazel_workspace,
    inspect_bazel_workspace,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: BazelWorkspaceInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: BazelCleanResult | None
    error: str | None = None


class _BazelMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Bazel 工作区维护")
        self._window.geometry("940x570")
        self._window.minsize(800, 500)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="请选择 Bazel workspace 进行精确检查.")
        self._workspace = tk.StringVar(value="")
        self._inventory: BazelWorkspaceInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Bazel 项目输出维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Bazel 的 output base 与具体 workspace 绑定. DevClean 不按 `_bazel_*` 或 "
                "`bazel-out` 名称直接删目录, 而是让 Bazel 自己确认 workspace 和 output_base."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        picker = ttk.Frame(container)
        picker.pack(fill=tk.X)
        ttk.Entry(picker, textvariable=self._workspace, state="readonly").pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
        )
        self._choose_button = ttk.Button(
            picker,
            text="选择 Bazel workspace...",
            command=self._choose_workspace,
        )
        self._choose_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="Bazel 确认结果", padding=10)
        details.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="尚未检查 workspace.",
            wraplength=860,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        ttk.Label(
            container,
            text=(
                "普通 clean: 删除该 workspace 的 action cache、execroot 和 build outputs, "
                "后续可重建; output_base 达到 2 GiB 时建议运行."
            ),
            wraplength=900,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            text=(
                "expunge: 删除整个 output_base, 包括 external repositories 和临时状态, "
                "并停止 Bazel server. 技术上由 Bazel 官方支持, 但可能导致大量重新下载/重建, "
                "所以永远由用户明确决定."
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
        self._expunge_button = ttk.Button(
            footer,
            text="完整 expunge...",
            command=lambda: self._start_cleanup(BazelCleanMode.EXPUNGE),
            state=tk.DISABLED,
        )
        self._expunge_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._clean_button = ttk.Button(
            footer,
            text="运行普通 clean",
            command=lambda: self._start_cleanup(BazelCleanMode.CLEAN),
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
        enabled = not busy and self._inventory is not None
        state = tk.NORMAL if enabled else tk.DISABLED
        self._clean_button.configure(state=state)
        self._expunge_button.configure(state=state)

    def _choose_workspace(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Bazel workspace 根目录",
            mustexist=True,
        )
        if not selected:
            return
        self._workspace.set(selected)
        self._start_inventory(Path(selected))

    def _start_inventory(self, workspace: Path) -> None:
        self._inventory = None
        self._set_busy(True)
        self._status.set("正在让 Bazel 确认 workspace 和 output_base...")

        def work() -> None:
            try:
                inventory = inspect_bazel_workspace(workspace)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Bazel-inventory", daemon=True).start()

    def _start_cleanup(self, mode: BazelCleanMode) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        if mode is BazelCleanMode.EXPUNGE:
            confirmed = messagebox.askyesno(
                "Bazel 完整 expunge",
                (
                    "这会让 Bazel 删除整个 output_base, 包括 external repositories 和"
                    " 所有构建缓存. 以后可能需要大量重新下载和重新构建.\n\n"
                    "确定继续吗?"
                ),
                parent=self._window,
            )
            if not confirmed:
                return
        self._set_busy(True)
        action = "expunge" if mode is BazelCleanMode.EXPUNGE else "clean"
        self._status.set(f"正在通过 Bazel 官方命令执行 {action}...")
        workspace = inventory.workspace

        def work() -> None:
            try:
                result = clean_bazel_workspace(workspace, mode)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(target=work, name="DevClean-Bazel-cleanup", daemon=True).start()

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
            self._details.configure(text=f"无法确认 Bazel workspace: {outcome}")
            self._status.set("未执行任何删除.")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"Bazel 维护失败: {outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                reclaimed = _format_bytes(outcome.result.reclaimed_bytes)
                self._status.set(f"Bazel 维护完成, 本次观察到释放约 {reclaimed}.")
                self._start_inventory(outcome.result.workspace)
        self._window.after(100, self._poll)

    def _render(self, inventory: BazelWorkspaceInventory) -> None:
        self._workspace.set(str(inventory.workspace))
        recommendation = (
            "建议运行普通 clean"
            if inventory.recommended_clean
            else "当前不默认建议运行普通 clean"
        )
        self._details.configure(
            text=(
                f"Release: {inventory.release}\n"
                f"Workspace: {inventory.workspace}\n"
                f"Output base: {inventory.output_base}\n"
                f"当前 output_base 占用: {_format_bytes(inventory.logical_bytes)}\n\n"
                f"判定: {recommendation}. expunge 永远需要用户明确确认."
            )
        )
        self._status.set("Bazel 已精确确认 workspace/output_base, 未进行 raw delete.")


def open_bazel_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _BazelMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_bazel_maintenance_dialog"]
