"""Podman stopped-container maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.podman_container_maintenance import (
    PodmanContainerEntry,
    PodmanContainerInventory,
    PodmanContainerRemoveResult,
    inspect_podman_containers,
    remove_podman_container,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: PodmanContainerInventory


@dataclass(frozen=True, slots=True)
class _RemoveEvent:
    result: PodmanContainerRemoveResult | None
    error: str | None = None


class _PodmanContainerMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Podman 已停止容器维护")
        self._window.geometry("1080x700")
        self._window.minsize(900, 580)
        self._events: queue.Queue[_InventoryEvent | _RemoveEvent | Exception] = queue.Queue()
        self._inventory: PodmanContainerInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 Podman machine。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Podman 已停止容器维护", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "只处理当前默认、能够证明属于本机 Podman machine 的连接。"
                "每次只允许删除一个已经停止且不属于 Pod 的精确容器；不会执行 system prune、"
                "不会强制删除，也不会删除卷。"
            ),
            wraplength=1030,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 8))
        ttk.Label(
            root,
            text=(
                "注意：Windows 上 Podman 数据位于 WSL/Hyper-V machine 内。这里显示的是 Podman 的逻辑存储信息；"
                "删除容器后 Windows 主机上的虚拟磁盘文件不一定同步缩小。"
            ),
            wraplength=1030,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 10))

        columns = ("name", "status", "writable", "rootfs", "volumes", "decision")
        self._tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
        self._tree.heading("name", text="容器")
        self._tree.heading("status", text="状态")
        self._tree.heading("writable", text="Writable")
        self._tree.heading("rootfs", text="RootFS")
        self._tree.heading("volumes", text="卷")
        self._tree.heading("decision", text="当前判定")
        self._tree.column("name", width=170, anchor=tk.W)
        self._tree.column("status", width=90, anchor=tk.W)
        self._tree.column("writable", width=100, anchor=tk.E)
        self._tree.column("rootfs", width=100, anchor=tk.E)
        self._tree.column("volumes", width=170, anchor=tk.W)
        self._tree.column("decision", width=360, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda event: self._update_remove_button())

        self._target_label = ttk.Label(root, text="", wraplength=1030, justify=tk.LEFT)
        self._target_label.pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(root, textvariable=self._status, wraplength=1030, justify=tk.LEFT).pack(anchor=tk.W, pady=(5, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="检查/刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._remove_button = ttk.Button(
            footer,
            text="删除选中的已停止容器…",
            command=self._confirm_remove,
            state=tk.DISABLED,
        )
        self._remove_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()
        self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._update_remove_button()

    def _selected(self) -> PodmanContainerEntry | None:
        if self._inventory is None:
            return None
        selected = self._tree.selection()
        if len(selected) != 1:
            return None
        container_id = selected[0]
        matches = [item for item in self._inventory.containers if item.container_id == container_id]
        return matches[0] if len(matches) == 1 else None

    def _update_remove_button(self) -> None:
        selected = self._selected()
        enabled = not self._busy and selected is not None and selected.executable
        self._remove_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在确认 Podman 默认 machine connection 并读取容器状态…")

        def work() -> None:
            try:
                inventory = inspect_podman_containers()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Podman-container-inventory", daemon=True).start()

    def _confirm_remove(self) -> None:
        if self._busy:
            return
        selected = self._selected()
        if selected is None or not selected.executable:
            return
        volumes = ", ".join(selected.volume_names) if selected.volume_names else "无命名卷"
        if not messagebox.askyesno(
            "确认删除 Podman 容器",
            (
                "将删除一个精确的已停止 Podman 容器。\n\n"
                f"名称：{selected.name}\n"
                f"ID：{selected.container_id}\n"
                f"镜像：{selected.image_name or selected.image_id}\n"
                f"Writable：{_format_bytes(selected.writable_size)}\n"
                f"关联卷：{volumes}\n\n"
                "容器 writable layer 可能包含唯一数据，删除后不可恢复。DevClean 不会使用 --force，"
                "也不会使用 --volumes，因此卷不会作为副作用被删除。\n\n确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set("正在重新验证 machine connection 和精确容器身份，然后调用 podman rm…")

        def work() -> None:
            try:
                result = remove_podman_container(selected)
            except Exception as error:
                self._events.put(_RemoveEvent(None, str(error)))
            else:
                self._events.put(_RemoveEvent(result))

        threading.Thread(target=work, name="DevClean-Podman-container-remove", daemon=True).start()

    def _poll(self) -> None:
        if not self._window.winfo_exists():
            return
        try:
            event = self._events.get_nowait()
        except queue.Empty:
            self._window.after(100, self._poll)
            return

        if isinstance(event, Exception):
            self._inventory = None
            self._clear_tree()
            self._target_label.configure(text="")
            self._status.set(f"Podman 检查失败/不可执行：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Podman 容器删除未完成：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._status.set(
                    f"已删除精确容器 {result.container.name} ({result.container.container_id[:12]})。"
                    "Podman 逻辑存储已重新检查；不承诺 Windows VM 磁盘同步缩小。"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _render(self, inventory: PodmanContainerInventory) -> None:
        self._clear_tree()
        target = inventory.target
        mode = "rootful" if target.rootful else "rootless"
        self._target_label.configure(
            text=(
                f"绑定 machine：{target.machine_name} / provider={target.vm_type} / connection={target.connection_name} "
                f"({mode}) / URI={target.connection_uri}"
            )
        )
        for entry in inventory.containers:
            decision = "USER_REVIEW" if entry.executable else f"保护：{entry.reason}"
            volumes = ", ".join(entry.volume_names) if entry.volume_names else "—"
            self._tree.insert(
                "",
                tk.END,
                iid=entry.container_id,
                values=(
                    entry.name,
                    entry.status,
                    _format_bytes(entry.writable_size),
                    _format_bytes(entry.rootfs_size),
                    volumes,
                    decision,
                ),
            )
        self._status.set(f"检查完成：{len(inventory.containers)} 个容器。只有符合边界的已停止 standalone container 可删除。")


def open_podman_container_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _PodmanContainerMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_podman_container_maintenance_dialog"]
