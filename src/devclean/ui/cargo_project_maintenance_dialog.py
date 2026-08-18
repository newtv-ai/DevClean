"""Project-aware Cargo target-directory maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.cargo_project_maintenance import (
    CargoCleanResult,
    CargoWorkspaceInventory,
    clean_cargo_workspace,
    inspect_cargo_workspace,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: CargoWorkspaceInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    result: CargoCleanResult | None
    error: str | None = None


class _CargoProjectMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Cargo 工作区 target 维护")
        self._window.geometry("980x600")
        self._window.minsize(820, 520)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="请选择 Cargo workspace 根目录。")
        self._workspace = tk.StringVar(value="")
        self._inventory: CargoWorkspaceInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Cargo 工作区 target 维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Cargo 官方把 target_directory 定义为生成产物目录，并提供 cargo clean。"
                "但 target 里既有中间缓存，也可能有 release 二进制、文档、package 输出等"
                "最终产物，所以完整清理属于“你来决定”，永不默认选择，也不需要 AI。"
            ),
            wraplength=940,
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
            text="选择 Cargo workspace…",
            command=self._choose_workspace,
        )
        self._choose_button.pack(side=tk.LEFT, padx=(8, 0))

        details = ttk.LabelFrame(container, text="Cargo 确认结果", padding=10)
        details.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="尚未检查 workspace。",
            wraplength=900,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        ttk.Label(
            container,
            text=(
                "执行边界：DevClean 用 cargo metadata --no-deps 确认 workspace_root 和"
                " target_directory；只有 target 位于所选 workspace 的本地固定磁盘子树内时"
                "才允许执行。外置/共享 target 只报告，避免 cargo clean 影响其他 workspace。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            text=(
                "真正执行时会再次解析 Cargo 配置，并把已确认的 --target-dir 显式传给"
                " cargo clean；不会直接递归删除 target，也不会处理 CARGO_HOME 的 registry/git。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._clean_button = ttk.Button(
            footer,
            text="运行 cargo clean…",
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
            and self._inventory.deletion_supported
            and self._inventory.logical_bytes > 0
        )
        self._clean_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _choose_workspace(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Cargo workspace 根目录",
            mustexist=True,
        )
        if not selected:
            return
        self._workspace.set(selected)
        self._start_inventory(Path(selected))

    def _start_inventory(self, workspace: Path) -> None:
        self._inventory = None
        self._set_busy(True)
        self._status.set("正在让 Cargo 确认 workspace 和有效 target_directory…")

        def work() -> None:
            try:
                inventory = inspect_cargo_workspace(workspace)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Cargo-workspace-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        inventory = self._inventory
        if (
            self._busy
            or inventory is None
            or not inventory.deletion_supported
            or inventory.logical_bytes <= 0
        ):
            return
        if not messagebox.askyesno(
            "确认 cargo clean",
            (
                f"将通过 Cargo 官方命令清理：\n{inventory.target_directory}\n\n"
                f"当前占用约 {_format_bytes(inventory.logical_bytes)}。\n"
                "完整 target 中可能包含 release 二进制、cargo doc、cargo package 输出以及"
                "增量编译缓存；源代码不会删除，但后续需要重新构建这些产物。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set("正在重新验证 Cargo workspace/target 并执行 cargo clean…")
        workspace = inventory.workspace

        def work() -> None:
            try:
                result = clean_cargo_workspace(workspace)
            except Exception as error:
                self._events.put(_CleanupEvent(None, str(error)))
            else:
                self._events.put(_CleanupEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Cargo-workspace-cleanup",
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
            self._details.configure(text=f"无法确认 Cargo workspace：{outcome}")
            self._status.set("未执行任何清理。")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"cargo clean 失败：{outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                reclaimed = _format_bytes(outcome.result.reclaimed_bytes)
                self._status.set(f"cargo clean 完成，释放约 {reclaimed}；正在重新检查…")
                self._busy = False
                self._start_inventory(outcome.result.workspace)
        self._window.after(100, self._poll)

    def _render(self, inventory: CargoWorkspaceInventory) -> None:
        self._workspace.set(str(inventory.workspace))
        if not inventory.deletion_supported:
            decision = (
                "target 不在 workspace 的本地固定磁盘子树内；可能是共享/外置目标，"
                "DevClean 只报告，不允许执行 cargo clean。"
            )
        elif inventory.logical_bytes == 0:
            decision = "当前 target 不存在或为空，无需清理。"
        elif inventory.worth_reviewing:
            decision = "占用较大，值得由你决定是否用重新构建时间换取磁盘空间。"
        else:
            decision = "占用不大；仍可由你手动清理，但不会默认推荐。"
        self._details.configure(
            text=(
                f"Cargo: {inventory.version}\n"
                f"workspace_root: {inventory.workspace}\n"
                f"manifest: {inventory.manifest}\n"
                f"target_directory: {inventory.target_directory}\n"
                f"当前占用: {_format_bytes(inventory.logical_bytes)}\n\n"
                f"判定: {decision}\n"
                "执行级别: USER_REVIEW，永不自动选择，不调用 AI。"
            )
        )
        self._status.set("Cargo 检查完成。")


def open_cargo_project_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _CargoProjectMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_cargo_project_maintenance_dialog"]
