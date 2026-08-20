"""Unified Docker storage/accounting and exact maintenance dialog."""

# ruff: noqa: E501, RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.docker_buildx_maintenance import (
    BuildxCacheInventory,
    BuildxPruneResult,
)
from devclean.core.docker_container_maintenance import DockerContainerEntry
from devclean.core.docker_image_maintenance import DockerImageEntry
from devclean.core.docker_unified_maintenance import (
    DockerUnifiedInventory,
    inspect_docker_unified,
    prune_reviewed_buildx_cache,
    prune_reviewed_docker_build_cache,
    remove_reviewed_docker_container,
    remove_reviewed_docker_image,
)

_RETENTION_HOURS = 168


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: DockerUnifiedInventory


@dataclass(frozen=True, slots=True)
class _ActionEvent:
    message: str | None
    error: str | None = None


class _DockerUnifiedMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Docker 本机存储维护")
        self._window.geometry("1180x780")
        self._window.minsize(980, 640)
        self._events: queue.Queue[_InventoryEvent | _ActionEvent | Exception] = queue.Queue()
        self._inventory: DockerUnifiedInventory | None = None
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 Docker daemon。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Docker 本机存储维护", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "把已经分别审计过的 Docker build cache、image、stopped container 和 volume 放到一个入口中。"
                "每条删除边界仍然独立：不会提供 docker system prune，也不会把 volume 当成缓存。"
            ),
            wraplength=1130,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 4))
        ttk.Label(
            root,
            text=(
                "Docker 的 Size/Reclaimable 是 daemon 的逻辑/共享层统计。共享 image layer、BuildKit cache 和 Docker Desktop VM/VHD "
                "会使这些数字与 Windows 物理可用空间不同；本页不承诺删除多少就立即释放多少主机磁盘。"
            ),
            wraplength=1130,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        self._target_label = ttk.Label(root, text="", wraplength=1130, justify=tk.LEFT)
        self._target_label.pack(anchor=tk.W, pady=(0, 8))

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)
        self._build_overview_tab(notebook)
        self._build_cache_tab(notebook)
        self._build_images_tab(notebook)
        self._build_containers_tab(notebook)
        self._build_volumes_tab(notebook)

        ttk.Label(root, textvariable=self._status, wraplength=1130, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(8, 0)
        )
        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._refresh_button = ttk.Button(
            footer, text="检查/刷新全部", command=self._start_inventory
        )
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)

        self._window.after(100, self._poll)

    def _build_overview_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="概览")
        ttk.Label(
            frame,
            text="来自 docker system df 的只读统计；Reclaimable 仅是 Docker 的估算，不是物理回收承诺。",
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("kind", "total", "active", "size", "reclaimable")
        self._overview_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, title, width in (
            ("kind", "类型", 180),
            ("total", "总数", 100),
            ("active", "Active", 100),
            ("size", "逻辑大小", 160),
            ("reclaimable", "Docker Reclaimable", 220),
        ):
            self._overview_tree.heading(column, text=title)
            self._overview_tree.column(column, width=width, anchor=tk.W)
        self._overview_tree.pack(fill=tk.BOTH, expand=True)

    def _build_cache_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Build Cache")
        ttk.Label(
            frame,
            text=(
                f"固定保留最近 {_RETENTION_HOURS // 24} 天。Classic builder 只调用 docker builder prune；"
                "Buildx 只对已经证明为本机的精确 builder 调用 buildx prune。两者都不会附带 --all，也不会触碰 image/container/volume。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        classic = ttk.Frame(frame)
        classic.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(classic, text="Classic builder cache", font=("Segoe UI", 10, "bold")).pack(
            side=tk.LEFT
        )
        self._classic_prune_button = ttk.Button(
            classic,
            text=f"清理超过 {_RETENTION_HOURS // 24} 天的 classic cache…",
            command=self._confirm_classic_prune,
            state=tk.DISABLED,
        )
        self._classic_prune_button.pack(side=tk.RIGHT)

        ttk.Label(frame, text="Buildx builders", font=("Segoe UI", 10, "bold")).pack(anchor=tk.W)
        columns = ("name", "driver", "nodes", "aged", "records", "decision")
        self._buildx_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        headings = (
            ("name", "Builder", 180),
            ("driver", "Driver", 120),
            ("nodes", "Nodes", 260),
            ("aged", f">{_RETENTION_HOURS // 24}天可回收", 130),
            ("records", "记录", 80),
            ("decision", "当前判定", 300),
        )
        for column, title, width in headings:
            self._buildx_tree.heading(column, text=title)
            self._buildx_tree.column(column, width=width, anchor=tk.W)
        self._buildx_tree.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self._buildx_tree.bind("<<TreeviewSelect>>", lambda event: self._update_action_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._buildx_prune_button = ttk.Button(
            actions,
            text="清理选中 Buildx 的老旧可回收 cache…",
            command=self._confirm_buildx_prune,
            state=tk.DISABLED,
        )
        self._buildx_prune_button.pack(side=tk.RIGHT)

    def _build_images_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Images")
        ttk.Label(
            frame,
            text=(
                "只允许 USER_REVIEW 删除当前没有 container 引用且最多一个 tag 的精确 image ID。"
                "执行固定为 image rm --no-prune；多 tag、仍被引用的 image 只报告。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("name", "size", "containers", "decision")
        self._image_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        for column, title, width in (
            ("name", "Tag / Image ID", 340),
            ("size", "逻辑大小", 120),
            ("containers", "Container 引用", 140),
            ("decision", "当前判定", 430),
        ):
            self._image_tree.heading(column, text=title)
            self._image_tree.column(column, width=width, anchor=tk.W)
        self._image_tree.pack(fill=tk.BOTH, expand=True)
        self._image_tree.bind("<<TreeviewSelect>>", lambda event: self._update_action_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._image_remove_button = ttk.Button(
            actions,
            text="删除选中的精确 image…",
            command=self._confirm_image_remove,
            state=tk.DISABLED,
        )
        self._image_remove_button.pack(side=tk.RIGHT)

    def _build_containers_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Containers")
        ttk.Label(
            frame,
            text=(
                "运行中的 container 永远保护。已停止 container 的 writable layer 可能包含唯一数据，因此仅 USER_REVIEW；"
                "删除不使用 --force，也不会附带删除 volume。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("name", "status", "writable", "rootfs", "volumes", "decision")
        self._container_tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse"
        )
        for column, title, width in (
            ("name", "容器", 180),
            ("status", "状态", 90),
            ("writable", "Writable", 100),
            ("rootfs", "RootFS", 100),
            ("volumes", "Volumes", 200),
            ("decision", "当前判定", 360),
        ):
            self._container_tree.heading(column, text=title)
            self._container_tree.column(column, width=width, anchor=tk.W)
        self._container_tree.pack(fill=tk.BOTH, expand=True)
        self._container_tree.bind("<<TreeviewSelect>>", lambda event: self._update_action_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._container_remove_button = ttk.Button(
            actions,
            text="删除选中的已停止 container…",
            command=self._confirm_container_remove,
            state=tk.DISABLED,
        )
        self._container_remove_button.pack(side=tk.RIGHT)

    def _build_volumes_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Volumes（只读）")
        ttk.Label(
            frame,
            text=(
                "Docker volume 是持久数据，即使当前没有 container 引用也可能保存数据库、工作目录或用户状态。"
                "本页只做精确 inventory，不提供 volume prune / rm 按钮。"
            ),
            wraplength=1080,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("name", "driver", "scope", "refs", "decision")
        self._volume_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, title, width in (
            ("name", "Volume", 250),
            ("driver", "Driver", 120),
            ("scope", "Scope", 100),
            ("refs", "Container 引用", 140),
            ("decision", "当前判定", 470),
        ):
            self._volume_tree.heading(column, text=title)
            self._volume_tree.column(column, width=width, anchor=tk.W)
        self._volume_tree.pack(fill=tk.BOTH, expand=True)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()
        self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._update_action_buttons()

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set(
            "正在绑定当前本机 Docker endpoint，并读取 storage / build cache / image / container / volume…"
        )

        def work() -> None:
            try:
                inventory = inspect_docker_unified(retention_hours=_RETENTION_HOURS)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Docker-unified-inventory", daemon=True).start()

    def _selected_image(self) -> DockerImageEntry | None:
        inventory = self._inventory
        selected = self._image_tree.selection()
        if inventory is None or len(selected) != 1:
            return None
        image_id = selected[0]
        matches = [entry for entry in inventory.images.images if entry.image_id == image_id]
        return matches[0] if len(matches) == 1 else None

    def _selected_container(self) -> DockerContainerEntry | None:
        inventory = self._inventory
        selected = self._container_tree.selection()
        if inventory is None or len(selected) != 1:
            return None
        container_id = selected[0]
        matches = [
            entry for entry in inventory.containers.containers if entry.container_id == container_id
        ]
        return matches[0] if len(matches) == 1 else None

    def _selected_buildx_cache(self) -> BuildxCacheInventory | None:
        inventory = self._inventory
        selected = self._buildx_tree.selection()
        if inventory is None or len(selected) != 1:
            return None
        name = selected[0]
        matches = [entry for entry in inventory.buildx_cache if entry.builder.name == name]
        return matches[0] if len(matches) == 1 else None

    def _update_action_buttons(self) -> None:
        inventory = self._inventory
        enabled = inventory is not None and not self._busy
        self._classic_prune_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

        image = self._selected_image()
        self._image_remove_button.configure(
            state=tk.NORMAL if enabled and image is not None and image.executable else tk.DISABLED
        )
        container = self._selected_container()
        self._container_remove_button.configure(
            state=(
                tk.NORMAL
                if enabled and container is not None and container.executable
                else tk.DISABLED
            )
        )
        buildx = self._selected_buildx_cache()
        self._buildx_prune_button.configure(
            state=(
                tk.NORMAL
                if enabled and buildx is not None and buildx.aged_reclaimable_bytes > 0
                else tk.DISABLED
            )
        )

    def _confirm_classic_prune(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        if not messagebox.askyesno(
            "确认清理 Docker classic build cache",
            (
                f"将对已经绑定的本机 endpoint 调用 docker builder prune，并只选择超过 {_RETENTION_HOURS // 24} 天的 build cache。\n\n"
                f"Endpoint：{inventory.target.endpoint}\n"
                "不会执行 docker system prune，不会删除 image、container 或 volume，也不会使用 --all。\n\n"
                "这些 cache 可以重建，但重建会消耗时间和网络/CPU。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        target = inventory.target
        self._run_action(
            "正在重新验证 exact Docker endpoint 并调用 builder prune…",
            lambda: (
                prune_reviewed_docker_build_cache(
                    target,
                    retention_hours=_RETENTION_HOURS,
                ),
                "Classic Docker build cache 清理完成；已重新检查 Docker 存储。",
            )[1],
            "DevClean-Docker-classic-prune",
        )

    def _confirm_buildx_prune(self) -> None:
        inventory = self._inventory
        selected = self._selected_buildx_cache()
        if self._busy or inventory is None or selected is None:
            return
        if not messagebox.askyesno(
            "确认清理 Buildx cache",
            (
                f"Builder：{selected.builder.name}\n"
                f"Driver：{selected.builder.driver}\n"
                f"超过 {_RETENTION_HOURS // 24} 天且当前可回收：{_format_bytes(selected.aged_reclaimable_bytes)}\n"
                f"记录：{selected.record_count}\n\n"
                "DevClean 会重新验证 builder node/endpoints 和 cache 状态，只调用该 builder 的 buildx prune；不会使用 --all。\n\n"
                "确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        target = inventory.target
        self._run_action(
            "正在重新验证 Buildx builder 和 cache 状态…",
            lambda: _buildx_action_message(prune_reviewed_buildx_cache(target, selected)),
            "DevClean-Docker-buildx-prune",
        )

    def _confirm_image_remove(self) -> None:
        inventory = self._inventory
        selected = self._selected_image()
        if self._busy or inventory is None or selected is None or not selected.executable:
            return
        label = selected.repo_tags[0] if selected.repo_tags else selected.image_id[:20]
        if not messagebox.askyesno(
            "确认删除 Docker image",
            (
                "将删除一个精确 Docker image。\n\n"
                f"Tag/ID：{label}\n"
                f"完整 ID：{selected.image_id}\n"
                f"逻辑大小：{_format_bytes(selected.logical_size)}\n\n"
                "当前没有 container 引用。DevClean 会重新验证 ID/tag/digest/container 引用，"
                "随后固定调用 image rm --no-prune；不会使用 --force，也不会连带 prune parent image。\n\n"
                "本地 image 可能用于离线工作或避免重新下载/构建。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        target = inventory.target
        self._run_action(
            "正在重新验证 exact image 和 reviewed endpoint…",
            lambda: (
                remove_reviewed_docker_image(target, selected),
                f"已删除精确 Docker image {label}；正在刷新统一存储视图。",
            )[1],
            "DevClean-Docker-image-remove",
        )

    def _confirm_container_remove(self) -> None:
        inventory = self._inventory
        selected = self._selected_container()
        if self._busy or inventory is None or selected is None or not selected.executable:
            return
        volumes = ", ".join(selected.volume_names) if selected.volume_names else "无命名 volume"
        if not messagebox.askyesno(
            "确认删除 Docker container",
            (
                "将删除一个精确的已停止 Docker container。\n\n"
                f"名称：{selected.name}\n"
                f"ID：{selected.container_id}\n"
                f"Image：{selected.image_ref or selected.image_id}\n"
                f"Writable：{_format_bytes(selected.writable_size)}\n"
                f"关联 volume：{volumes}\n\n"
                "Writable layer 可能包含唯一数据。DevClean 不使用 --force，也不会删除 volume。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            return
        target = inventory.target
        self._run_action(
            "正在重新验证 exact stopped container 和 reviewed endpoint…",
            lambda: (
                remove_reviewed_docker_container(target, selected),
                f"已删除精确 Docker container {selected.name}；正在刷新统一存储视图。",
            )[1],
            "DevClean-Docker-container-remove",
        )

    def _run_action(self, status: str, work: Callable[[], str], thread_name: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set(status)

        def runner() -> None:
            try:
                message = work()
            except Exception as error:
                self._events.put(_ActionEvent(None, str(error)))
            else:
                self._events.put(_ActionEvent(message))

        threading.Thread(target=runner, name=thread_name, daemon=True).start()

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
            self._clear_all()
            self._target_label.configure(text="")
            self._status.set(f"Docker 检查失败/不可执行：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Docker 维护未完成：{event.error}")
                self._set_busy(False)
            else:
                self._status.set(event.message or "Docker 维护完成。")
                self._set_busy(False)
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear_tree(self, tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def _clear_all(self) -> None:
        for tree in (
            self._overview_tree,
            self._buildx_tree,
            self._image_tree,
            self._container_tree,
            self._volume_tree,
        ):
            self._clear_tree(tree)

    def _render(self, inventory: DockerUnifiedInventory) -> None:
        self._clear_all()
        target = inventory.target
        context = target.context_name or "(DOCKER_HOST)"
        self._target_label.configure(
            text=(
                f"用户审核对象：context={context} / endpoint={target.endpoint} / source={target.source}。"
                "后续 destructive action 会固定到这个 endpoint，而不是再次跟随默认 context。"
            )
        )

        for index, row in enumerate(inventory.storage.rows):
            self._overview_tree.insert(
                "",
                tk.END,
                iid=f"usage-{index}",
                values=(row.kind, row.total, row.active, row.size, row.reclaimable),
            )

        cache_by_name = {
            cache_entry.builder.name: cache_entry for cache_entry in inventory.buildx_cache
        }
        for index, builder in enumerate(inventory.builders):
            cache = cache_by_name.get(builder.name)
            nodes = ", ".join(f"{node.name}@{node.endpoint}" for node in builder.nodes) or "—"
            if not builder.executable:
                decision = f"保护：{builder.reason}"
                aged = "—"
                records = "—"
            elif cache is None:
                decision = "只报告：未取得 cache accounting"
                aged = "—"
                records = "—"
            else:
                aged = _format_bytes(cache.aged_reclaimable_bytes)
                records = str(cache.record_count)
                decision = (
                    "可维护（>=1 GiB，建议检查）"
                    if cache.worth_maintaining
                    else "可维护，但当前收益较低"
                )
            self._buildx_tree.insert(
                "",
                tk.END,
                iid=builder.name if builder.name else f"builder-{index}",
                values=(builder.name, builder.driver, nodes, aged, records, decision),
            )

        for image_entry in inventory.images.images:
            label = image_entry.repo_tags[0] if image_entry.repo_tags else image_entry.image_id[:20]
            decision = "USER_REVIEW" if image_entry.executable else f"保护：{image_entry.reason}"
            self._image_tree.insert(
                "",
                tk.END,
                iid=image_entry.image_id,
                values=(
                    label,
                    _format_bytes(image_entry.logical_size),
                    len(image_entry.container_ids),
                    decision,
                ),
            )

        for container_entry in inventory.containers.containers:
            decision = (
                "USER_REVIEW" if container_entry.executable else f"保护：{container_entry.reason}"
            )
            volumes = (
                ", ".join(container_entry.volume_names) if container_entry.volume_names else "—"
            )
            self._container_tree.insert(
                "",
                tk.END,
                iid=container_entry.container_id,
                values=(
                    container_entry.name,
                    container_entry.status,
                    _format_bytes(container_entry.writable_size),
                    _format_bytes(container_entry.rootfs_size),
                    volumes,
                    decision,
                ),
            )

        for index, volume_entry in enumerate(inventory.volumes.volumes):
            self._volume_tree.insert(
                "",
                tk.END,
                iid=f"volume-{index}",
                values=(
                    volume_entry.name,
                    volume_entry.driver,
                    volume_entry.scope or "—",
                    len(volume_entry.container_ids),
                    volume_entry.reason,
                ),
            )

        buildx_note = (
            f"；Buildx 检查失败：{inventory.buildx_error}" if inventory.buildx_error else ""
        )
        self._status.set(
            "检查完成："
            f"{len(inventory.images.images)} images / "
            f"{len(inventory.containers.containers)} containers / "
            f"{len(inventory.volumes.volumes)} volumes / "
            f"{len(inventory.builders)} Buildx builders{buildx_note}。"
        )
        self._update_action_buttons()


def _buildx_action_message(result: BuildxPruneResult) -> str:
    return (
        f"Buildx builder {result.builder.name} 清理完成；"
        f"观察到 aged reclaimable 从 {_format_bytes(result.before_reclaimable_bytes)} "
        f"变为 {_format_bytes(result.after_reclaimable_bytes)}。正在刷新统一存储视图。"
    )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


def open_docker_unified_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _DockerUnifiedMaintenanceDialog(parent).show()


__all__ = ["open_docker_unified_maintenance_dialog"]
