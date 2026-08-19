"""Podman exact image maintenance dialog."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.podman_image_maintenance import (
    PodmanImageEntry,
    PodmanImageInventory,
    PodmanImageRemoveResult,
    inspect_podman_images,
    remove_podman_image,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: PodmanImageInventory


@dataclass(frozen=True, slots=True)
class _RemoveEvent:
    result: PodmanImageRemoveResult | None
    error: str | None = None


class _PodmanImageMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Podman 镜像维护")
        self._window.geometry("1160x720")
        self._window.minsize(940, 600)
        self._events: queue.Queue[_InventoryEvent | _RemoveEvent | Exception] = queue.Queue()
        self._inventory: PodmanImageInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 Podman 镜像。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Podman 镜像维护", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "每次只允许删除一个能够完整证明未被 Podman/Buildah/CRI-O 容器引用、没有 child image、"
                "不是只读镜像、不是 manifest list 且最多只有一个 tag 的精确镜像。"
                "镜像即使 dangling、很大或很旧，也不会自动删除。"
            ),
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 6))
        ttk.Label(
            root,
            text=(
                "删除命令固定使用 podman image rm --no-prune <完整 image ID>；不会使用 --force，"
                "也不会顺带删除 dangling parent。Windows 上实际数据位于 Podman machine 内，"
                "这里不承诺 Windows 虚拟磁盘文件同步缩小。"
            ),
            wraplength=1110,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 10))

        columns = ("name", "id", "size", "refs", "children", "flags", "decision")
        self._tree = ttk.Treeview(root, columns=columns, show="headings", selectmode="browse")
        self._tree.heading("name", text="Tag / 镜像")
        self._tree.heading("id", text="Image ID")
        self._tree.heading("size", text="Size")
        self._tree.heading("refs", text="容器引用")
        self._tree.heading("children", text="Children")
        self._tree.heading("flags", text="特殊状态")
        self._tree.heading("decision", text="当前判定")
        self._tree.column("name", width=245, anchor=tk.W)
        self._tree.column("id", width=110, anchor=tk.W)
        self._tree.column("size", width=90, anchor=tk.E)
        self._tree.column("refs", width=80, anchor=tk.CENTER)
        self._tree.column("children", width=70, anchor=tk.CENTER)
        self._tree.column("flags", width=160, anchor=tk.W)
        self._tree.column("decision", width=350, anchor=tk.W)
        self._tree.pack(fill=tk.BOTH, expand=True)
        self._tree.bind("<<TreeviewSelect>>", lambda event: self._update_remove_button())

        self._target_label = ttk.Label(root, text="", wraplength=1110, justify=tk.LEFT)
        self._target_label.pack(anchor=tk.W, pady=(8, 0))
        self._proof_label = ttk.Label(root, text="", wraplength=1110, justify=tk.LEFT)
        self._proof_label.pack(anchor=tk.W, pady=(4, 0))
        ttk.Label(root, textvariable=self._status, wraplength=1110, justify=tk.LEFT).pack(anchor=tk.W, pady=(5, 0))

        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(footer, text="检查/刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        self._remove_button = ttk.Button(
            footer,
            text="删除选中的精确镜像…",
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

    def _selected(self) -> PodmanImageEntry | None:
        if self._inventory is None:
            return None
        selected = self._tree.selection()
        if len(selected) != 1:
            return None
        image_id = selected[0]
        matches = [item for item in self._inventory.images if item.image_id == image_id]
        return matches[0] if len(matches) == 1 else None

    def _update_remove_button(self) -> None:
        selected = self._selected()
        enabled = not self._busy and selected is not None and selected.executable
        self._remove_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在绑定 Podman machine，并核对镜像、普通容器和 external container 引用…")

        def work() -> None:
            try:
                inventory = inspect_podman_images()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Podman-image-inventory", daemon=True).start()

    def _confirm_remove(self) -> None:
        if self._busy:
            return
        selected = self._selected()
        inventory = self._inventory
        if selected is None or inventory is None or not selected.executable:
            return
        reviewed_target = inventory.target
        tag = selected.repo_tags[0] if selected.repo_tags else "<untagged>"
        if not messagebox.askyesno(
            "确认删除 Podman 镜像",
            (
                "将删除一个精确 Podman 镜像。\n\n"
                f"Machine：{reviewed_target.machine_name}\n"
                f"Connection：{reviewed_target.connection_name}\n"
                f"Tag：{tag}\n"
                f"Image ID：{selected.image_id}\n"
                f"大小：{_format_bytes(selected.size)}\n\n"
                "DevClean 已确认当前没有普通 Podman container 或 Buildah/CRI-O external container 引用它，"
                "并且它不是 read-only image、manifest list，也没有 child image。\n\n"
                "操作会失去这个本地镜像内容；以后可能需要重新 pull/build。"
                "命令不会使用 --force，并固定使用 --no-prune，不会顺带删除父镜像。\n\n确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        self._set_busy(True)
        self._status.set("正在重新验证 machine、镜像身份、child 和全部 container 引用，然后执行精确 image rm…")

        def work() -> None:
            try:
                result = remove_podman_image(selected, reviewed_target)
            except Exception as error:
                self._events.put(_RemoveEvent(None, str(error)))
            else:
                self._events.put(_RemoveEvent(result))

        threading.Thread(target=work, name="DevClean-Podman-image-remove", daemon=True).start()

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
            self._proof_label.configure(text="")
            self._status.set(f"Podman 镜像检查失败/不可执行：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Podman 镜像删除未完成：{event.error}")
                self._set_busy(False)
            elif event.result is not None:
                result = event.result
                self._status.set(
                    f"已删除精确 image {result.image.image_id[:12]}。"
                    "Podman 逻辑存储已重新检查；不承诺 Windows machine 虚拟磁盘同步缩小。"
                )
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear_tree(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)

    def _render(self, inventory: PodmanImageInventory) -> None:
        self._clear_tree()
        target = inventory.target
        mode = "rootful" if target.rootful else "rootless"
        self._target_label.configure(
            text=(
                f"绑定 machine：{target.machine_name} / provider={target.vm_type} / "
                f"connection={target.connection_name} ({mode}) / URI={target.connection_uri}"
            )
        )
        if inventory.reference_proof_complete:
            self._proof_label.configure(
                text="Container 引用证明：完整。已同时检查普通 Podman containers 与 external Buildah/CRI-O containers。"
            )
        else:
            self._proof_label.configure(
                text=(
                    "Container 引用证明：不完整，因此所有 image 都只显示、不可删除。原因："
                    f"{inventory.reference_proof_reason}"
                )
            )
        for entry in inventory.images:
            name = entry.repo_tags[0] if entry.repo_tags else "<untagged>"
            refs = len(entry.podman_container_ids) + len(entry.external_container_ids)
            flags: list[str] = []
            if entry.read_only:
                flags.append("read-only")
            if entry.manifest_list:
                flags.append("manifest-list")
            if len(entry.repo_tags) > 1:
                flags.append(f"{len(entry.repo_tags)} tags")
            decision = "USER_REVIEW" if entry.executable else f"保护：{entry.reason}"
            self._tree.insert(
                "",
                tk.END,
                iid=entry.image_id,
                values=(
                    name,
                    entry.image_id[:12],
                    _format_bytes(entry.size),
                    refs,
                    len(entry.child_ids),
                    ", ".join(flags) if flags else "—",
                    decision,
                ),
            )
        self._status.set(
            f"检查完成：{len(inventory.images)} 个 image。只有边界完整且经用户确认的精确 image 才可删除。"
        )


def open_podman_image_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _PodmanImageMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_podman_image_maintenance_dialog"]
