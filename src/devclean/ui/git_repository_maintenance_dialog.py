"""Git repository maintenance UI with separate Git and Git LFS decision lanes."""

# ruff: noqa: RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from devclean.core.git_repository_maintenance import (
    GitLfsPrunePreview,
    GitLfsPruneResult,
    GitMaintenanceResult,
    GitRepositoryInventory,
    inspect_git_repository,
    preview_git_lfs_prune,
    run_git_automatic_maintenance,
    run_git_lfs_prune,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    inventory: GitRepositoryInventory


@dataclass(frozen=True, slots=True)
class _MaintenanceEvent:
    result: GitMaintenanceResult | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PreviewEvent:
    preview: GitLfsPrunePreview | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _PruneEvent:
    result: GitLfsPruneResult | None
    error: str | None = None


_Event = _InventoryEvent | _MaintenanceEvent | _PreviewEvent | _PruneEvent | Exception


class _GitRepositoryMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Git / Git LFS 存储维护")
        self._window.geometry("1040x760")
        self._window.minsize(880, 620)
        self._events: queue.Queue[_Event] = queue.Queue()
        self._root_text = tk.StringVar(value="")
        self._status = tk.StringVar(value="请选择 Git worktree 根目录。")
        self._inventory: GitRepositoryInventory | None = None
        self._preview: GitLfsPrunePreview | None = None
        self._busy = False

        container = ttk.Frame(self._window, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            container,
            text="Git / Git LFS 存储维护",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            container,
            text=(
                "这里不删除 .git 里的路径。Git 对象优化交给 git maintenance --auto; "
                "Git LFS 只使用自己的 prune 规则，并把是否丢弃可重新下载的本地副本交给用户。"
            ),
            wraplength=990,
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
            text="选择 Git 仓库…",
            command=self._choose_root,
        )
        self._choose_button.pack(side=tk.LEFT, padx=(8, 0))

        git_frame = ttk.LabelFrame(container, text="Git 对象库自动维护", padding=10)
        git_frame.pack(fill=tk.X, pady=(10, 0))
        self._git_details = ttk.Label(
            git_frame,
            text="尚未检查。",
            wraplength=960,
            justify=tk.LEFT,
        )
        self._git_details.pack(anchor=tk.W)
        self._git_button = ttk.Button(
            git_frame,
            text="运行 Git 自动维护",
            command=self._start_git_maintenance,
            state=tk.DISABLED,
        )
        self._git_button.pack(anchor=tk.E, pady=(8, 0))

        lfs_frame = ttk.LabelFrame(container, text="Git LFS 本地对象", padding=10)
        lfs_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self._lfs_details = ttk.Label(
            lfs_frame,
            text="尚未检查。",
            wraplength=960,
            justify=tk.LEFT,
        )
        self._lfs_details.pack(anchor=tk.W)

        controls = ttk.Frame(lfs_frame)
        controls.pack(fill=tk.X, pady=(8, 0))
        self._preview_button = ttk.Button(
            controls,
            text="先运行 LFS prune 预览",
            command=self._start_lfs_preview,
            state=tk.DISABLED,
        )
        self._preview_button.pack(side=tk.LEFT)
        self._prune_button = ttk.Button(
            controls,
            text="执行已预览的 LFS prune…",
            command=self._start_lfs_prune,
            state=tk.DISABLED,
        )
        self._prune_button.pack(side=tk.LEFT, padx=(8, 0))

        self._preview_text = tk.Text(lfs_frame, height=10, wrap=tk.WORD, state=tk.DISABLED)
        self._preview_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        ttk.Label(
            container,
            text=(
                "保护边界: 不运行 git clean / reset, 不直接删除 objects、reflog、worktree 元数据，"
                "不使用 git lfs prune --force。显式 lfs.storage 或 alternate object database "
                "只报告，不获得执行权限。"
            ),
            wraplength=990,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(8, 0))
        ttk.Label(
            container,
            textvariable=self._status,
            wraplength=990,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        footer = ttk.Frame(container)
        footer.pack(fill=tk.X, pady=(10, 0))
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
            title="选择 Git worktree 根目录",
            mustexist=True,
        )
        if selected:
            self._root_text.set(selected)
            self._start_inventory(Path(selected))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._choose_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        inventory = self._inventory
        git_ready = (
            not busy
            and inventory is not None
            and inventory.maintenance_executable
            and inventory.maintenance_needed is True
        )
        lfs_ready = not busy and inventory is not None and inventory.lfs.prune_supported
        preview_matches = (
            self._preview is not None
            and inventory is not None
            and self._preview.workspace == inventory.workspace
        )
        self._git_button.configure(state=tk.NORMAL if git_ready else tk.DISABLED)
        self._preview_button.configure(state=tk.NORMAL if lfs_ready else tk.DISABLED)
        self._prune_button.configure(
            state=tk.NORMAL if lfs_ready and preview_matches else tk.DISABLED
        )

    def _start_inventory(self, root: Path) -> None:
        self._preview = None
        self._set_preview_text("")
        self._set_busy(True)
        self._status.set("正在让 Git 确认 worktree、对象库、maintenance 和 LFS 存储边界…")

        def work() -> None:
            try:
                inventory = inspect_git_repository(root)
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(inventory))

        threading.Thread(target=work, name="DevClean-Git-inventory", daemon=True).start()

    def _start_git_maintenance(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        self._set_busy(True)
        self._status.set("正在重新确认仓库并运行 git maintenance run --auto…")

        def work() -> None:
            try:
                result = run_git_automatic_maintenance(inventory.workspace)
            except Exception as error:
                self._events.put(_MaintenanceEvent(None, str(error)))
            else:
                self._events.put(_MaintenanceEvent(result))

        threading.Thread(target=work, name="DevClean-Git-maintenance", daemon=True).start()

    def _start_lfs_preview(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        self._preview = None
        self._set_busy(True)
        self._status.set("正在运行 Git LFS 官方 dry-run，并远端验证 reachable / unreachable 候选…")

        def work() -> None:
            try:
                preview = preview_git_lfs_prune(inventory.workspace)
            except Exception as error:
                self._events.put(_PreviewEvent(None, str(error)))
            else:
                self._events.put(_PreviewEvent(preview))

        threading.Thread(target=work, name="DevClean-Git-LFS-preview", daemon=True).start()

    def _start_lfs_prune(self) -> None:
        inventory = self._inventory
        preview = self._preview
        if self._busy or inventory is None or preview is None:
            return
        confirmed = messagebox.askyesno(
            "确认 Git LFS prune",
            (
                "Git LFS 已完成 dry-run 预览。继续后只会调用 Git LFS 自己的 prune，"
                "并使用远端验证；不会使用 --force。\n\n"
                f"当前本地 LFS 存储约 {_format_bytes(preview.before_bytes)}。"
                "删除旧本地副本后，未来 checkout 可能需要重新下载。\n\n确定继续吗?"
            ),
            parent=self._window,
            icon=messagebox.WARNING,
        )
        if not confirmed:
            return
        self._set_busy(True)
        self._status.set("正在重新确认 LFS 边界并执行远端验证的 git lfs prune…")

        def work() -> None:
            try:
                result = run_git_lfs_prune(inventory.workspace)
            except Exception as error:
                self._events.put(_PruneEvent(None, str(error)))
            else:
                self._events.put(_PruneEvent(result))

        threading.Thread(target=work, name="DevClean-Git-LFS-prune", daemon=True).start()

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
            self._status.set(f"Git 仓库检查失败: {event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.inventory
            self._render(event.inventory)
            self._set_busy(False)
        elif isinstance(event, _MaintenanceEvent):
            if event.error is not None:
                self._status.set(f"Git 自动维护失败: {event.error}")
                self._set_busy(False)
            elif event.result is not None:
                self._status.set(
                    "Git 自动维护完成，观察到对象库释放约 "
                    f"{_format_bytes(event.result.reclaimed_bytes)}。正在重新统计…"
                )
                self._start_inventory(event.result.workspace)
        elif isinstance(event, _PreviewEvent):
            if event.error is not None:
                self._preview = None
                self._status.set(f"Git LFS prune 预览失败: {event.error}")
            elif event.preview is not None:
                self._preview = event.preview
                self._set_preview_text(event.preview.output or "Git LFS 未报告可 prune 的对象。")
                self._status.set(
                    "LFS dry-run 完成。结果来自 Git LFS 自己的规则和远端验证；"
                    "如要实际删除，仍需你再次确认。"
                )
            self._set_busy(False)
        else:
            if event.error is not None:
                self._status.set(f"Git LFS prune 失败: {event.error}")
                self._set_busy(False)
            elif event.result is not None:
                self._status.set(
                    "Git LFS prune 完成，释放约 "
                    f"{_format_bytes(event.result.reclaimed_bytes)}。正在重新统计…"
                )
                self._start_inventory(event.result.workspace)
        self._window.after(100, self._poll)

    def _render(self, inventory: GitRepositoryInventory) -> None:
        self._root_text.set(str(inventory.workspace))
        need_text = {
            True: "Git 判断: 已达到自动维护阈值",
            False: "Git 判断: 当前无需自动维护",
            None: "Git 判断: 无法确认 maintenance is-needed",
        }[inventory.maintenance_needed]
        alternates = (
            ", ".join(str(path) for path in inventory.alternates) if inventory.alternates else "无"
        )
        self._git_details.configure(
            text=(
                f"Git: {inventory.version}\n"
                f"worktree: {inventory.workspace}\n"
                f"common dir: {inventory.common_dir}\n"
                f"objects: {inventory.objects_dir} ({_format_bytes(inventory.object_bytes)})\n"
                f"alternate objects: {alternates}\n"
                f"{need_text}\n"
                f"执行边界: {inventory.maintenance_reason}"
            )
        )

        lfs = inventory.lfs
        self._lfs_details.configure(
            text=(
                f"Git LFS: {lfs.version}\n"
                f"LocalMediaDir: {lfs.storage_dir or '未确认'}\n"
                f"本地 LFS 占用: {_format_bytes(lfs.logical_bytes)}\n"
                f"显式 lfs.storage: {'是' if lfs.custom_storage else '否'}\n"
                f"判定: {lfs.reason}\n"
                "决策级别: USER_REVIEW。即使可从远端恢复，也永不默认删除。"
            )
        )
        self._status.set("Git 仓库检查完成。已知语义在本地处理，不调用 AI。")

    def _set_preview_text(self, text: str) -> None:
        self._preview_text.configure(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)
        if text:
            self._preview_text.insert("1.0", text)
        self._preview_text.configure(state=tk.DISABLED)


def open_git_repository_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _GitRepositoryMaintenanceDialog(parent).show()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


__all__ = ["open_git_repository_maintenance_dialog"]
