"""vcpkg storage UI: known semantics, explicit user intent, no AI."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.vcpkg_maintenance import (
    VcpkgCleanResult,
    VcpkgStorageEntry,
    VcpkgStorageInventory,
    VcpkgStorageKind,
    clean_vcpkg_storage,
    inspect_vcpkg_root,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: VcpkgStorageInventory


@dataclass(frozen=True, slots=True)
class _CleanupEvent:
    results: tuple[VcpkgCleanResult, ...]
    error: str | None = None


class _VcpkgMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("vcpkg 存储维护")
        self._window.geometry("980x660")
        self._window.minsize(840, 560)
        self._events: queue.Queue[_InventoryEvent | _CleanupEvent | Exception] = queue.Queue()
        self._status = tk.StringVar(value="请选择 vcpkg 根目录。")
        self._root_text = tk.StringVar(value="")
        self._inventory: VcpkgStorageInventory | None = None
        self._choices: dict[VcpkgStorageKind, tk.BooleanVar] = {}
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="vcpkg 存储维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "Microsoft 明确区分 packages、buildtrees、downloads 和二进制缓存。"
                "DevClean 不把它们混成一个“vcpkg 缓存”，也不交给 AI；"
                "是否删除由你决定。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        picker = ttk.Frame(container)
        picker.pack(fill=tk.X)
        ttk.Entry(picker, textvariable=self._root_text, state="readonly").pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
        )
        self._choose_button = ttk.Button(
            picker,
            text="选择 vcpkg 根目录…",
            command=self._choose_root,
        )
        self._choose_button.pack(side=tk.LEFT, padx=(8, 0))

        self._rows = ttk.Frame(container)
        self._rows.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        ttk.Label(
            container,
            text=(
                "特别注意：buildtrees 可能包含通过 --editable 保留并修改的源码；"
                "downloads 和二进制缓存也具有离线/减少重建时间的价值。"
                "默认不会勾选任何项。"
            ),
            wraplength=940,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
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
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _choose_root(self) -> None:
        if self._busy:
            return
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择包含 .vcpkg-root 和 vcpkg.exe 的目录",
            mustexist=True,
        )
        if selected:
            self._root_text.set(selected)
            self._start_inventory(Path(selected))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._choose_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._clean_button.configure(
            state=tk.DISABLED if busy or self._inventory is None else tk.NORMAL
        )

    def _start_inventory(self, root: Path) -> None:
        self._set_busy(True)
        self._status.set("正在确认 vcpkg 实例并统计各类存储…")

        def work() -> None:
            try:
                self._events.put(_InventoryEvent(inspect_vcpkg_root(root)))
            except Exception as error:
                self._events.put(error)

        threading.Thread(
            target=work,
            name="DevClean-vcpkg-inventory",
            daemon=True,
        ).start()

    def _start_cleanup(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        selected = [
            entry
            for entry in inventory.entries
            if entry.executable
            and entry.exists
            and (choice := self._choices.get(entry.kind)) is not None
            and choice.get()
        ]
        if not selected:
            messagebox.showinfo(
                "vcpkg 存储维护",
                "没有勾选可执行的 vcpkg 存储。",
            )
            return
        warning = (
            "将永久删除你勾选的 vcpkg 临时/构建存储。\n\n"
            "如果包含 buildtrees，请确认没有 --editable 端口开发修改；"
            "downloads 删除后可能需要重新下载。\n\n确定继续吗？"
        )
        if not messagebox.askyesno(
            "确认 vcpkg 清理",
            warning,
            icon=messagebox.WARNING,
        ):
            return
        self._set_busy(True)
        self._status.set(f"正在精确清理 {len(selected)} 项 vcpkg 存储…")

        def work(entries: tuple[VcpkgStorageEntry, ...] = tuple(selected)) -> None:
            results: list[VcpkgCleanResult] = []
            error_text: str | None = None
            for entry in entries:
                try:
                    results.append(clean_vcpkg_storage(inventory.root, entry.kind))
                except Exception as error:
                    error_text = str(error)
                    break
            self._events.put(_CleanupEvent(tuple(results), error_text))

        threading.Thread(
            target=work,
            name="DevClean-vcpkg-cleanup",
            daemon=True,
        ).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(event, Exception):
            self._status.set(f"vcpkg 操作失败：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            reclaimed = sum(item.reclaimed_bytes for item in event.results)
            suffix = "" if event.error is None else f"；随后停止：{event.error}"
            self._status.set(
                f"已完成 {len(event.results)} 项，释放约 {_format_bytes(reclaimed)}"
                f"{suffix}；正在重新统计…"
            )
            self._busy = False
            root = (
                self._inventory.root if self._inventory is not None else Path(self._root_text.get())
            )
            self._start_inventory(root)
        self._window.after(100, self._poll)

    def _render(self, inventory: VcpkgStorageInventory) -> None:
        for child in self._rows.winfo_children():
            child.destroy()
        self._choices.clear()
        visible = [entry for entry in inventory.entries if entry.exists]
        visible.sort(key=lambda item: item.logical_bytes, reverse=True)
        for index, entry in enumerate(visible):
            title = f"{_kind_label(entry.kind)} · {_format_bytes(entry.logical_bytes)}"
            row = ttk.LabelFrame(self._rows, text=title, padding=9)
            row.grid(row=index, column=0, sticky="ew", pady=(0, 7))
            self._rows.columnconfigure(0, weight=1)
            choice = tk.BooleanVar(value=False)
            self._choices[entry.kind] = choice
            ttk.Checkbutton(
                row,
                text="清理" if entry.executable else "仅报告",
                variable=choice,
                state=tk.NORMAL if entry.executable else tk.DISABLED,
            ).grid(row=0, column=0, rowspan=3, sticky="nw", padx=(0, 12))
            ttk.Label(
                row,
                text=entry.reason,
                wraplength=760,
                justify=tk.LEFT,
            ).grid(row=0, column=1, sticky="w")
            ttk.Label(
                row,
                text=str(entry.path),
                wraplength=760,
                justify=tk.LEFT,
            ).grid(row=1, column=1, sticky="w", pady=(4, 0))
            row.columnconfigure(1, weight=1)
        total = sum(entry.logical_bytes for entry in visible)
        self._status.set(
            f"已确认 {inventory.version}；定位 {len(visible)} 类存储，"
            f"共约 {_format_bytes(total)}。全部由用户决定，AI 不参与。"
        )


def open_vcpkg_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _VcpkgMaintenanceDialog(parent).show()


def _kind_label(kind: VcpkgStorageKind) -> str:
    return {
        VcpkgStorageKind.PACKAGES: "packages 包暂存",
        VcpkgStorageKind.BUILDTREES: "buildtrees 构建树",
        VcpkgStorageKind.DOWNLOADS: "downloads 下载资产",
        VcpkgStorageKind.DEFAULT_BINARY_CACHE: "默认二进制缓存",
    }[kind]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_vcpkg_maintenance_dialog"]
