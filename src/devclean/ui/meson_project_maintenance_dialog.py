"""Exact configured Meson build-directory maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.meson_project_maintenance import (
    MesonBuildInventory,
    MesonBuildRemovalResult,
    inspect_meson_build,
    remove_meson_build_directory,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: MesonBuildInventory


@dataclass(frozen=True, slots=True)
class _RemovalEvent:
    result: MesonBuildRemovalResult | None
    error: str | None = None


class _MesonProjectMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Meson 构建目录维护")
        self._window.geometry("1020x650")
        self._window.minsize(860, 560)
        self._events: queue.Queue[_InventoryEvent | _RemovalEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="请选择 Meson 源码根目录和已配置的构建目录。")
        self._source = tk.StringVar(value="")
        self._build = tk.StringVar(value="")
        self._inventory: MesonBuildInventory | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Meson 构建目录维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Meson 把源码树和构建树明确分离，并把完整删除已配置 build tree 作为"
                "重新开始构建的正常方式。但构建目录可能包含当前二进制、测试日志和大量"
                "已经完成的编译工作，因此这里属于 USER_REVIEW：永不自动选择，也不调用 AI。"
            ),
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        self._source_button = self._picker_row(
            container,
            "源码根目录",
            self._source,
            "选择源码…",
            self._choose_source,
        )
        self._build_button = self._picker_row(
            container,
            "构建目录",
            self._build,
            "选择构建目录…",
            self._choose_build,
        )

        details = ttk.LabelFrame(container, text="Meson 确认结果", padding=10)
        details.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._details = ttk.Label(
            details,
            text="尚未检查。",
            wraplength=940,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        ttk.Label(
            container,
            text=(
                "执行边界：DevClean 不认可 build/out 等目录名。必须看到 Meson 自己的"
                " meson-private/coredata.dat，并通过 meson introspect --buildsystem-files"
                " 把该构建目录重新绑定到你选择的顶层 meson.build。"
            ),
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(10, 0))
        ttk.Label(
            container,
            text=(
                "真正删除前会再次运行 Meson introspection、检查工具版本、源码/构建目录身份、"
                "本地固定磁盘和进程状态，然后只对这个精确 build tree 使用 handle-bound 删除。"
                "不会运行 meson setup --wipe，也不会把 meson compile --clean 的后端行为当成"
                "已经审计过的通用清理。"
            ),
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=980,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._remove_button = ttk.Button(
            footer,
            text="删除已确认的构建目录…",
            command=self._start_removal,
            state=tk.DISABLED,
        )
        self._remove_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def _picker_row(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        button_text: str,
        command: Callable[[], None],
    ) -> ttk.Button:
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=(2, 0))
        ttk.Label(row, text=f"{label}：", width=12).pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=variable, state="readonly").pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
        )
        button = ttk.Button(row, text=button_text, command=command)
        button.pack(side=tk.LEFT, padx=(8, 0))
        return button

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self._source_button.configure(state=state)
        self._build_button.configure(state=state)
        enabled = (
            not busy
            and self._inventory is not None
            and self._inventory.deletion_supported
            and self._inventory.logical_bytes > 0
        )
        self._remove_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _choose_source(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 Meson 源码根目录（包含顶层 meson.build）",
            mustexist=True,
        )
        if not selected:
            return
        self._source.set(selected)
        self._maybe_start_inventory()

    def _choose_build(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择已配置的 Meson 构建目录",
            mustexist=True,
        )
        if not selected:
            return
        self._build.set(selected)
        self._maybe_start_inventory()

    def _maybe_start_inventory(self) -> None:
        source = self._source.get().strip()
        build = self._build.get().strip()
        if not source or not build:
            self._inventory = None
            self._set_busy(False)
            self._status.set("还需要选择源码根目录和构建目录。")
            return
        self._start_inventory(Path(source), Path(build))

    def _start_inventory(self, source: Path, build: Path) -> None:
        self._inventory = None
        self._set_busy(True)
        self._status.set("正在让 Meson 确认配置身份和 build-system 文件…")

        def work() -> None:
            try:
                inventory = inspect_meson_build(source, build)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(
            target=work,
            name="DevClean-Meson-build-inventory",
            daemon=True,
        ).start()

    def _start_removal(self) -> None:
        inventory = self._inventory
        if (
            self._busy
            or inventory is None
            or not inventory.deletion_supported
            or inventory.logical_bytes <= 0
        ):
            return
        if not messagebox.askyesno(
            "确认删除 Meson 构建目录",
            (
                f"将完整删除这个已经由 Meson 确认的构建树：\n{inventory.build_root}\n\n"
                f"当前占用约 {_format_bytes(inventory.logical_bytes)}。\n"
                "源码树不会删除，但当前二进制、对象文件、测试日志、配置状态和增量编译"
                "结果都会消失；下次需要重新 meson setup/构建。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set("正在重新验证 Meson 配置和目录身份，然后执行精确删除…")
        source = inventory.source_root
        build = inventory.build_root

        def work() -> None:
            try:
                result = remove_meson_build_directory(source, build)
            except Exception as error:
                self._events.put(_RemovalEvent(None, str(error)))
            else:
                self._events.put(_RemovalEvent(result))

        threading.Thread(
            target=work,
            name="DevClean-Meson-build-removal",
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
            self._details.configure(text=f"无法确认 Meson 构建目录：{outcome}")
            self._status.set("未执行任何清理。")
            self._set_busy(False)
        elif isinstance(outcome, _InventoryEvent):
            self._inventory = outcome.inventory
            self._render(outcome.inventory)
            self._set_busy(False)
        else:
            if outcome.error is not None:
                self._status.set(f"Meson 构建目录删除失败：{outcome.error}")
                self._set_busy(False)
            elif outcome.result is not None:
                self._inventory = None
                self._details.configure(
                    text=(
                        f"已删除：{outcome.result.build_root}\n"
                        f"观察到释放约 {_format_bytes(outcome.result.reclaimed_bytes)}。\n"
                        "源码目录保持不变。"
                    )
                )
                self._status.set("Meson 构建目录维护完成。")
                self._set_busy(False)
        self._window.after(100, self._poll)

    def _render(self, inventory: MesonBuildInventory) -> None:
        self._source.set(str(inventory.source_root))
        self._build.set(str(inventory.build_root))
        if not inventory.deletion_supported:
            decision = (
                "配置身份已确认，但存储边界不是可批准的本地固定磁盘普通目录；"
                "DevClean 只报告，不允许删除。"
            )
        elif inventory.logical_bytes == 0:
            decision = "构建目录当前没有可计量文件；无需为了磁盘空间删除。"
        elif inventory.worth_reviewing:
            decision = "占用较大，值得由你决定是否用重新配置/编译时间换取磁盘空间。"
        else:
            decision = "占用不大；仍可手动删除，但不会默认推荐。"
        self._details.configure(
            text=(
                f"Meson: {inventory.version}\n"
                f"source_root: {inventory.source_root}\n"
                f"build_root: {inventory.build_root}\n"
                f"build-system 文件数: {len(inventory.buildsystem_files)}\n"
                f"当前占用: {_format_bytes(inventory.logical_bytes)}\n\n"
                f"判定: {decision}\n"
                "执行级别: USER_REVIEW，永不自动选择，不调用 AI。"
            )
        )
        self._status.set("Meson 检查完成。")


def open_meson_project_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _MesonProjectMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_meson_project_maintenance_dialog"]
