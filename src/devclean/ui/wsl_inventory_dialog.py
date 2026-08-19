"""Read-only WSL distribution storage overview."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from devclean.core.wsl_inventory import WslInventory, inspect_wsl


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: WslInventory | None
    error: str | None = None


class _WslInventoryDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("WSL 发行版存储概览")
        self._window.geometry("900x620")
        self._window.minsize(760, 520)
        self._events: queue.Queue[_InventoryEvent] = queue.Queue()
        self._status = tk.StringVar(value="正在向 WSL 查询已注册发行版…")

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="WSL 发行版存储概览",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "WSL 发行版虚拟磁盘包含完整 Linux 文件系统和用户数据，不是缓存。"
                "这里仅使用 WSL 自己的只读命令确认发行版和运行状态，不查找或删除 ext4.vhdx。"
            ),
            wraplength=850,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 10))

        details = ttk.LabelFrame(container, text="WSL 状态", padding=10)
        details.pack(fill=tk.X)
        self._details = ttk.Label(
            details,
            text="等待检查…",
            wraplength=820,
            justify=tk.LEFT,
        )
        self._details.pack(anchor=tk.W)

        distro_frame = ttk.LabelFrame(container, text="已注册发行版", padding=10)
        distro_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._tree = ttk.Treeview(
            distro_frame,
            columns=("name", "state", "decision"),
            show="headings",
            height=13,
        )
        self._tree.heading("name", text="发行版")
        self._tree.heading("state", text="状态")
        self._tree.heading("decision", text="DevClean 判定")
        self._tree.column("name", width=260, anchor=tk.W)
        self._tree.column("state", width=110, anchor=tk.W)
        self._tree.column("decision", width=360, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            container,
            text=(
                "保护边界: 不调用 wsl --unregister，不终止发行版，不猜测 VHD 路径，"
                "不从 Windows 侧删除 Linux cache/build 目录。后续清理必须走发行版内部对应工具。"
            ),
            wraplength=850,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=850,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(footer, text="重新检查", command=self._start).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)
        self._start()

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()

    def _start(self) -> None:
        self._status.set("正在向 WSL 查询已注册发行版和 running 状态…")

        def work() -> None:
            try:
                inventory = inspect_wsl()
            except Exception as error:
                self._events.put(_InventoryEvent(None, str(error)))
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-WSL-inventory", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return
        if event.error is not None:
            self._status.set(f"WSL 检查失败: {event.error}")
        elif event.inventory is not None:
            self._render(event.inventory)
        self._window.after(100, self._poll)

    def _render(self, inventory: WslInventory) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        for distro in inventory.distributions:
            self._tree.insert(
                "",
                tk.END,
                values=(
                    distro.name,
                    "运行中" if distro.running else "已停止",
                    "KEEP / REPORT_ONLY — 发行版是持久数据",
                ),
            )
        version = inventory.version_text or "当前 WSL 未提供 --version 输出"
        status = inventory.status_text or "当前 WSL 未提供 --status 输出"
        self._details.configure(text=f"{version}\n\n{status}")
        self._status.set(
            f"已确认 {len(inventory.distributions)} 个发行版。"
            "当前入口完全只读，不授予任何 VHD 或发行版删除权限。"
        )


def open_wsl_inventory_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _WslInventoryDialog(parent).show()


__all__ = ["open_wsl_inventory_dialog"]
