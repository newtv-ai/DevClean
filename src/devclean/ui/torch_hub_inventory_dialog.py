"""Read-only PyTorch Hub storage overview."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, ttk

from devclean.core.torch_hub_inventory import (
    TorchHubEntryKind,
    TorchHubInventory,
    default_torch_hub_root,
    inventory_torch_hub,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: TorchHubInventory | None
    error: str | None = None


class _TorchHubInventoryDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("PyTorch Hub 存储概览")
        self._window.geometry("1040x680")
        self._window.minsize(860, 560)
        self._events: queue.Queue[_InventoryEvent] = queue.Queue()
        candidate = default_torch_hub_root()
        self._root_path = tk.StringVar(value=str(candidate.path))
        self._candidate_note = tk.StringVar(
            value=f"默认候选来源: {candidate.source}。{candidate.note}"
        )
        self._status = tk.StringVar(value="等待只读检查…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="PyTorch Hub 存储概览",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "当前 PyTorch Hub 没有可供 DevClean 使用的公共缓存清单/精确删除 API。"
                "这里仅检查用户明确选择的 Hub 根目录，不执行 Python、hubconf.py、网络请求或删除。"
            ),
            wraplength=990,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))

        path_frame = ttk.LabelFrame(container, text="检查根目录", padding=10)
        path_frame.pack(fill=tk.X)
        row = ttk.Frame(path_frame)
        row.pack(fill=tk.X)
        ttk.Entry(row, textvariable=self._root_path).pack(
            side=tk.LEFT,
            fill=tk.X,
            expand=True,
        )
        ttk.Button(row, text="选择目录…", command=self._choose_root).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )
        ttk.Label(
            path_frame,
            textvariable=self._candidate_note,
            wraplength=960,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        items_frame = ttk.LabelFrame(container, text="顶层存储对象", padding=10)
        items_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._tree = ttk.Treeview(
            items_frame,
            columns=("name", "kind", "size", "files", "decision"),
            show="headings",
            height=13,
        )
        self._tree.heading("name", text="对象")
        self._tree.heading("kind", text="解释")
        self._tree.heading("size", text="逻辑大小")
        self._tree.heading("files", text="文件数")
        self._tree.heading("decision", text="DevClean 判定")
        self._tree.column("name", width=260, anchor=tk.W)
        self._tree.column("kind", width=220, anchor=tk.W)
        self._tree.column("size", width=110, anchor=tk.E)
        self._tree.column("files", width=80, anchor=tk.E)
        self._tree.column("decision", width=170, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", self._show_selected)

        self._details = ttk.Label(
            container,
            text=(
                "选择一行可查看保护原因。repo-like 目录名不会被反解成 owner/repo/ref；"
                "checkpoints 文件也不会被假定为一定可重新下载。"
            ),
            wraplength=990,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=990,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="重新检查", command=self._start).pack(
            side=tk.RIGHT,
            padx=(8, 0),
        )
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._inventory: TorchHubInventory | None = None
        self._window.after(100, self._poll)
        self._start()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(
            parent=self._window,
            title="选择 PyTorch Hub 根目录",
            mustexist=True,
        )
        if not selected:
            return
        self._root_path.set(selected)
        self._candidate_note.set(
            "当前使用用户明确选择的目录。DevClean 只读检查，不把目录名称转换成删除权限。"
        )
        self._start()

    def _start(self) -> None:
        root_text = self._root_path.get().strip()
        if not root_text:
            self._status.set("请先选择一个 PyTorch Hub 根目录。")
            return
        self._status.set("正在只读统计 PyTorch Hub 顶层对象；不会跟随 reparse/symlink/cloud 边界…")

        def work() -> None:
            try:
                inventory = inventory_torch_hub(Path(root_text))
            except Exception as error:
                self._events.put(_InventoryEvent(None, str(error)))
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-TorchHub-inventory", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if event.error is not None:
            self._status.set(f"PyTorch Hub 检查失败: {event.error}")
        elif event.inventory is not None:
            self._render(event.inventory)
        self._window.after(100, self._poll)

    def _render(self, inventory: TorchHubInventory) -> None:
        self._inventory = inventory
        for item in self._tree.get_children():
            self._tree.delete(item)

        for index, entry in enumerate(inventory.entries):
            self._tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    entry.name,
                    _kind_text(entry.kind),
                    _format_bytes(entry.logical_bytes),
                    str(entry.file_count),
                    entry.decision.value,
                ),
            )

        if not inventory.exists:
            self._status.set(
                "默认/选择的 Hub 根目录不存在。运行中的 Python 可能使用 torch.hub.set_dir() 指向其他目录。"
            )
            return
        if not inventory.scannable:
            self._status.set("根目录是重解析/云边界，DevClean 拒绝穿越。" + _warnings_text(inventory))
            return
        self._status.set(
            f"只读统计 {len(inventory.entries)} 个顶层对象、"
            f"{inventory.total_file_count} 个普通文件、"
            f"{_format_bytes(inventory.total_logical_bytes)} 逻辑大小。"
            "这些数字不是删除候选，也不等于可回收物理空间。"
            + _warnings_text(inventory)
        )

    def _show_selected(self, _event: tk.Event[tk.Misc]) -> None:
        inventory = self._inventory
        selection = self._tree.selection()
        if inventory is None or not selection:
            return
        try:
            entry = inventory.entries[int(selection[0])]
        except (ValueError, IndexError):
            return
        boundary = "；该对象边界被跳过" if entry.boundary_skipped else ""
        self._details.configure(
            text=f"{entry.path}\n{entry.reason}{boundary}"
        )


def _kind_text(kind: TorchHubEntryKind) -> str:
    values = {
        TorchHubEntryKind.CHECKPOINTS: "checkpoints 权重区",
        TorchHubEntryKind.TRUST_STATE: "信任状态",
        TorchHubEntryKind.REPOSITORY_OR_UNKNOWN: "repo-like / 未知目录",
        TorchHubEntryKind.DOWNLOAD_TEMP_OR_UNKNOWN: "下载临时 / 未知 zip",
        TorchHubEntryKind.OTHER: "其他未定义对象",
    }
    return values[kind]


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{amount:.1f} TiB"


def _warnings_text(inventory: TorchHubInventory) -> str:
    if not inventory.warnings:
        return ""
    return " 警告: " + "；".join(inventory.warnings)


def open_torch_hub_inventory_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _TorchHubInventoryDialog(parent).show()


__all__ = ["open_torch_hub_inventory_dialog"]
