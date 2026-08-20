"""Exact Hugging Face Hub repo/revision maintenance dialog."""

# ruff: noqa: E501, RUF001

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from devclean.core.huggingface_maintenance import (
    HuggingFaceDeletePreview,
    HuggingFaceHubInventory,
    HuggingFaceHubPrunePreview,
    HuggingFaceHubRepo,
    HuggingFaceHubRevision,
    HuggingFaceStorageInventory,
    execute_huggingface_hub_prune,
    inventory_huggingface_hub_cache,
    inventory_huggingface_storage,
    preview_huggingface_hub_prune,
    preview_huggingface_repo_removal,
    preview_huggingface_revision_removal,
    remove_huggingface_repo,
    remove_huggingface_revision,
)


@dataclass(frozen=True, slots=True)
class _InventoryEvent:
    hub: HuggingFaceHubInventory
    storage: HuggingFaceStorageInventory


@dataclass(frozen=True, slots=True)
class _RepoPreviewEvent:
    inventory: HuggingFaceHubInventory
    repo: HuggingFaceHubRepo
    preview: HuggingFaceDeletePreview


@dataclass(frozen=True, slots=True)
class _RevisionPreviewEvent:
    inventory: HuggingFaceHubInventory
    revision: HuggingFaceHubRevision
    preview: HuggingFaceDeletePreview


@dataclass(frozen=True, slots=True)
class _PrunePreviewEvent:
    inventory: HuggingFaceHubInventory
    preview: HuggingFaceHubPrunePreview


@dataclass(frozen=True, slots=True)
class _ActionEvent:
    message: str | None
    error: str | None = None


class _HuggingFaceHubMaintenanceDialog:
    def __init__(self, parent: tk.Tk | tk.Toplevel) -> None:
        self._parent = parent
        self._window = tk.Toplevel(parent)
        self._window.title("Hugging Face Hub 缓存维护")
        self._window.geometry("1220x790")
        self._window.minsize(1020, 650)
        self._events: queue.Queue[
            _InventoryEvent
            | _RepoPreviewEvent
            | _RevisionPreviewEvent
            | _PrunePreviewEvent
            | _ActionEvent
            | Exception
        ] = queue.Queue()
        self._inventory: HuggingFaceHubInventory | None = None
        self._storage: HuggingFaceStorageInventory | None = None
        self._repo_rows: dict[str, HuggingFaceHubRepo] = {}
        self._revision_rows: dict[str, HuggingFaceHubRevision] = {}
        self._busy = False
        self._status = tk.StringVar(value="尚未检查 Hugging Face Hub cache。")

        root = ttk.Frame(self._window, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        ttk.Label(root, text="Hugging Face Hub 缓存维护", font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            root,
            text=(
                "模型、数据集和 revision 虽然可以重新下载，但可能是离线工作集、复现实验或精确 commit 快照。"
                "因此 exact repo / revision 删除全部是 USER_REVIEW；不会按年龄、大小或“可重新下载”自动删。"
            ),
            wraplength=1170,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 4))
        ttk.Label(
            root,
            text=(
                "删除只调用官方 hf cache rm；prune 只调用官方 hf cache prune。每次先用 vendor dry-run 证明范围，再重新验证 cache root、hf CLI、"
                "repo/revision 状态和相关进程。HF_HOME/token、Xet cache、assets cache 不做原始目录删除。"
            ),
            wraplength=1170,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 4))
        ttk.Label(
            root,
            text=(
                "界面中的 repo/revision Size 和 dry-run freed size 是 Hugging Face 自己的 cache accounting。snapshot 可共享 blob，Windows 上还可能因 symlink 能力而复制；"
                "这些数字不能当成 Windows 物理可用空间的一比一承诺。"
            ),
            wraplength=1170,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        self._boundary_label = ttk.Label(root, text="", wraplength=1170, justify=tk.LEFT)
        self._boundary_label.pack(anchor=tk.W, pady=(0, 8))

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)
        self._build_repo_tab(notebook)
        self._build_revision_tab(notebook)
        self._build_other_cache_tab(notebook)

        ttk.Label(root, textvariable=self._status, wraplength=1170, justify=tk.LEFT).pack(anchor=tk.W, pady=(8, 0))
        footer = ttk.Frame(root)
        footer.pack(fill=tk.X, pady=(10, 0))
        self._prune_button = ttk.Button(
            footer,
            text="审查 detached revision + incomplete 下载的 vendor prune…",
            command=self._start_prune_preview,
            state=tk.DISABLED,
        )
        self._prune_button.pack(side=tk.LEFT)
        self._refresh_button = ttk.Button(footer, text="检查/刷新", command=self._start_inventory)
        self._refresh_button.pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(footer, text="关闭", command=self._window.destroy).pack(side=tk.RIGHT)
        self._window.after(100, self._poll)

    def _build_repo_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Cached Repositories")
        ttk.Label(
            frame,
            text="选择一个 exact cached repo。删除会清掉该 repo 当前所有 cached revisions，但不会删除 token、Xet/assets cache 或其他 repo。",
            wraplength=1120,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("id", "type", "size", "revisions", "refs", "modified", "decision")
        self._repo_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (
            ("id", "Repo cache ID", 300),
            ("type", "Type", 90),
            ("size", "Vendor Size", 105),
            ("revisions", "Revisions", 80),
            ("refs", "Refs", 210),
            ("modified", "Last Modified", 140),
            ("decision", "判定", 245),
        ):
            self._repo_tree.heading(column, text=title)
            self._repo_tree.column(column, width=width, anchor=tk.W)
        self._repo_tree.pack(fill=tk.BOTH, expand=True)
        self._repo_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._repo_remove_button = ttk.Button(
            actions,
            text="删除选中的 exact cached repo…",
            command=self._start_repo_preview,
            state=tk.DISABLED,
        )
        self._repo_remove_button.pack(side=tk.RIGHT)

    def _build_revision_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Revisions")
        ttk.Label(
            frame,
            text=(
                "revision 删除只接受 vendor 返回的完整 40-hex commit，并且该 hash 必须在完整 inventory 中全局唯一。"
                "如果 hf 报告 cache inconsistency/warning，或同一 hash 出现在多个 repo，revision 删除会 fail closed。"
            ),
            wraplength=1120,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("repo", "revision", "size", "refs", "modified", "decision")
        self._revision_tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (
            ("repo", "Repo", 250),
            ("revision", "Full commit", 310),
            ("size", "Vendor Size", 100),
            ("refs", "Refs", 180),
            ("modified", "Last Modified", 140),
            ("decision", "判定", 250),
        ):
            self._revision_tree.heading(column, text=title)
            self._revision_tree.column(column, width=width, anchor=tk.W)
        self._revision_tree.pack(fill=tk.BOTH, expand=True)
        self._revision_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        actions = ttk.Frame(frame)
        actions.pack(fill=tk.X, pady=(8, 0))
        self._revision_remove_button = ttk.Button(
            actions,
            text="删除选中的 exact revision…",
            command=self._start_revision_preview,
            state=tk.DISABLED,
        )
        self._revision_remove_button.pack(side=tk.RIGHT)

    def _build_other_cache_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, padding=8)
        notebook.add(frame, text="Xet / Assets（只读）")
        ttk.Label(
            frame,
            text=(
                "HF_HOME 是混合状态，可能包含 token。Xet 是独立传输/chunk cache，assets 是下游库的独立 cache。"
                "当前只显示它们的根目录和非跟随 symlink 的粗略逻辑占用，不授予 raw delete 权限。"
            ),
            wraplength=1120,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 6))
        columns = ("kind", "path", "size", "decision")
        self._other_tree = ttk.Treeview(frame, columns=columns, show="headings")
        for column, title, width in (
            ("kind", "Cache", 100),
            ("path", "Exact root", 650),
            ("size", "粗略逻辑大小", 130),
            ("decision", "判定", 300),
        ):
            self._other_tree.heading(column, text=title)
            self._other_tree.column(column, width=width, anchor=tk.W)
        self._other_tree.pack(fill=tk.BOTH, expand=True)

    def show(self) -> None:
        self._window.transient(self._parent)
        self._window.grab_set()
        self._window.focus_set()
        self._start_inventory()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._update_buttons()

    def _start_inventory(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._status.set("正在绑定 exact Hub cache root 和 hf CLI，并执行两次 aggregate + revision JSON inventory…")

        def work() -> None:
            try:
                hub = inventory_huggingface_hub_cache()
                storage = inventory_huggingface_storage()
            except Exception as error:
                self._events.put(error)
            else:
                self._events.put(_InventoryEvent(hub, storage))

        threading.Thread(target=work, name="DevClean-HF-Hub-inventory", daemon=True).start()

    def _selected_repo(self) -> HuggingFaceHubRepo | None:
        selected = self._repo_tree.selection()
        return self._repo_rows.get(selected[0]) if len(selected) == 1 else None

    def _selected_revision(self) -> HuggingFaceHubRevision | None:
        selected = self._revision_tree.selection()
        return self._revision_rows.get(selected[0]) if len(selected) == 1 else None

    def _update_buttons(self) -> None:
        enabled = self._inventory is not None and not self._busy
        repo = self._selected_repo()
        revision = self._selected_revision()
        self._repo_remove_button.configure(
            state=tk.NORMAL if enabled and repo is not None and repo.deletion_supported else tk.DISABLED
        )
        self._revision_remove_button.configure(
            state=(
                tk.NORMAL
                if enabled and revision is not None and revision.deletion_supported
                else tk.DISABLED
            )
        )
        self._prune_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _start_repo_preview(self) -> None:
        inventory = self._inventory
        repo = self._selected_repo()
        if self._busy or inventory is None or repo is None or not repo.deletion_supported:
            return
        self._set_busy(True)
        self._status.set(f"正在 fresh revalidate + vendor dry-run exact repo {repo.cache_id}…")

        def work() -> None:
            try:
                preview = preview_huggingface_repo_removal(inventory, repo)
            except Exception as error:
                self._events.put(_ActionEvent(None, str(error)))
            else:
                self._events.put(_RepoPreviewEvent(inventory, repo, preview))

        threading.Thread(target=work, name="DevClean-HF-repo-preview", daemon=True).start()

    def _start_revision_preview(self) -> None:
        inventory = self._inventory
        revision = self._selected_revision()
        if self._busy or inventory is None or revision is None or not revision.deletion_supported:
            return
        self._set_busy(True)
        self._status.set(f"正在 fresh revalidate + vendor dry-run revision {revision.commit_hash}…")

        def work() -> None:
            try:
                preview = preview_huggingface_revision_removal(inventory, revision)
            except Exception as error:
                self._events.put(_ActionEvent(None, str(error)))
            else:
                self._events.put(_RevisionPreviewEvent(inventory, revision, preview))

        threading.Thread(target=work, name="DevClean-HF-revision-preview", daemon=True).start()

    def _start_prune_preview(self) -> None:
        inventory = self._inventory
        if self._busy or inventory is None:
            return
        self._set_busy(True)
        self._status.set("正在 fresh revalidate 并 dry-run vendor prune…")

        def work() -> None:
            try:
                preview = preview_huggingface_hub_prune(inventory)
            except Exception as error:
                self._events.put(_ActionEvent(None, str(error)))
            else:
                self._events.put(_PrunePreviewEvent(inventory, preview))

        threading.Thread(target=work, name="DevClean-HF-prune-preview", daemon=True).start()

    def _confirm_repo(self, event: _RepoPreviewEvent) -> None:
        self._set_busy(False)
        repo = event.repo
        preview = event.preview
        if not messagebox.askyesno(
            "确认删除 Hugging Face cached repo",
            (
                f"Repo：{repo.cache_id}\n"
                f"当前 revisions：{len(repo.revisions)}\n"
                f"Vendor inventory size：{repo.vendor_size}\n"
                f"Vendor dry-run：{preview.repos} repo / {preview.revisions} revisions / {preview.vendor_size}\n\n"
                "这会移除该 repo 当前全部 cached revisions。它们可能是离线工作集或复现实验所需快照；之后需要重新下载。\n\n"
                "DevClean 会在真正删除前再次完整 inventory 和 dry-run；任何变化都会拒绝执行。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            self._status.set("已取消 cached repo 删除。")
            return
        self._run_action(
            "正在再次验证 exact repo / root / hf CLI / process，并执行 vendor rm…",
            lambda: _repo_result_message(remove_huggingface_repo(event.inventory, repo, preview)),
            "DevClean-HF-repo-remove",
        )

    def _confirm_revision(self, event: _RevisionPreviewEvent) -> None:
        self._set_busy(False)
        revision = event.revision
        preview = event.preview
        refs = ", ".join(revision.refs) if revision.refs else "detached / exact-commit snapshot"
        if not messagebox.askyesno(
            "确认删除 Hugging Face cached revision",
            (
                f"Repo：{revision.cache_id}\n"
                f"Commit：{revision.commit_hash}\n"
                f"Refs：{refs}\n"
                f"Vendor revision size：{revision.vendor_size}\n"
                f"Vendor dry-run：{preview.repos} repo / {preview.revisions} revision / {preview.vendor_size}\n\n"
                "如果这是该 repo 最后一个 revision，vendor 可能移除整个 cached repo。"
                "该 commit 可能用于离线/复现实验；之后需要重新下载。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            self._status.set("已取消 cached revision 删除。")
            return
        self._run_action(
            "正在再次验证 unique full commit / root / hf CLI / process，并执行 vendor rm…",
            lambda: _revision_result_message(
                remove_huggingface_revision(event.inventory, revision, preview)
            ),
            "DevClean-HF-revision-remove",
        )

    def _confirm_prune(self, event: _PrunePreviewEvent) -> None:
        self._set_busy(False)
        preview = event.preview
        if preview.revisions == 0 and preview.incomplete == 0:
            self._status.set("Vendor dry-run：没有 detached revision 或 incomplete 下载可 prune。")
            return
        if not messagebox.askyesno(
            "确认 Hugging Face vendor prune",
            (
                f"Vendor dry-run：{preview.revisions} detached revisions + {preview.incomplete} incomplete downloads\n"
                f"Vendor expected freed：{preview.vendor_size}\n\n"
                "注意：detached revision 不等于“没价值”。按精确 commit hash 下载的快照可能没有 branch/tag ref，却仍可能是你刻意保留的离线或复现实验工作集。\n\n"
                "因此这不是自动清理。DevClean 会在执行前再次完整 inventory + dry-run，只有结果完全一致才调用 hf cache prune。确定继续吗？"
            ),
            icon=messagebox.WARNING,
            parent=self._window,
        ):
            self._status.set("已取消 Hugging Face vendor prune。")
            return
        self._run_action(
            "正在再次验证 prune 范围并执行 vendor prune…",
            lambda: _prune_result_message(
                execute_huggingface_hub_prune(event.inventory, preview)
            ),
            "DevClean-HF-prune",
        )

    def _run_action(self, status: str, work: object, thread_name: str) -> None:
        if self._busy:
            return
        if not callable(work):
            raise TypeError("work must be callable")
        self._set_busy(True)
        self._status.set(status)

        def runner() -> None:
            try:
                message = str(work())
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
            self._storage = None
            self._clear()
            self._boundary_label.configure(text="")
            self._status.set(f"Hugging Face Hub 检查失败/不可执行：{event}")
            self._set_busy(False)
        elif isinstance(event, _InventoryEvent):
            self._inventory = event.hub
            self._storage = event.storage
            self._render(event.hub, event.storage)
            self._set_busy(False)
        elif isinstance(event, _RepoPreviewEvent):
            self._confirm_repo(event)
        elif isinstance(event, _RevisionPreviewEvent):
            self._confirm_revision(event)
        elif isinstance(event, _PrunePreviewEvent):
            self._confirm_prune(event)
        else:
            self._set_busy(False)
            if event.error is not None:
                self._status.set(f"Hugging Face Hub 维护未完成：{event.error}")
            else:
                self._status.set(event.message or "Hugging Face Hub 维护完成。")
                self._start_inventory()
        self._window.after(100, self._poll)

    def _clear(self) -> None:
        self._repo_rows.clear()
        self._revision_rows.clear()
        for tree in (self._repo_tree, self._revision_tree, self._other_tree):
            for item in tree.get_children():
                tree.delete(item)

    def _render(
        self,
        hub: HuggingFaceHubInventory,
        storage: HuggingFaceStorageInventory,
    ) -> None:
        self._clear()
        self._boundary_label.configure(
            text=(
                f"Exact Hub root：{hub.hub_root.path} | hf CLI：{hub.hf_tool.path} | "
                f"revision unique-proof={'完整' if hub.revision_delete_proof_complete else '不完整/只允许 repo 级审查'}"
            )
        )

        for index, repo in enumerate(hub.repos):
            iid = f"repo-{index}"
            self._repo_rows[iid] = repo
            refs = ", ".join(repo.refs) if repo.refs else "—"
            decision = "USER_REVIEW" if repo.deletion_supported else f"保护：{repo.reason}"
            self._repo_tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    repo.cache_id,
                    repo.repo_type,
                    repo.vendor_size,
                    len(repo.revisions),
                    refs,
                    repo.last_modified or "—",
                    decision,
                ),
            )
            for revision in repo.revisions:
                revision_iid = f"revision-{len(self._revision_rows)}"
                self._revision_rows[revision_iid] = revision
                revision_refs = ", ".join(revision.refs) if revision.refs else "detached"
                revision_decision = (
                    "USER_REVIEW"
                    if revision.deletion_supported
                    else f"保护：{revision.reason}"
                )
                self._revision_tree.insert(
                    "",
                    tk.END,
                    iid=revision_iid,
                    values=(
                        repo.cache_id,
                        revision.commit_hash,
                        revision.vendor_size,
                        revision_refs,
                        revision.last_modified or "—",
                        revision_decision,
                    ),
                )

        for index, cache in enumerate(storage.caches):
            if cache.kind.value == "hub":
                continue
            self._other_tree.insert(
                "",
                tk.END,
                iid=f"cache-{index}",
                values=(
                    cache.kind.value,
                    str(cache.path),
                    _format_bytes(cache.logical_bytes),
                    "REPORT_ONLY / vendor-managed; no raw delete",
                ),
            )

        warning_note = f"；hf warnings={len(hub.warnings)}" if hub.warnings else ""
        self._status.set(
            f"检查完成：{len(hub.repos)} cached repos / {len(self._revision_rows)} revisions{warning_note}。"
            "Repo 删除仍需 USER_REVIEW；revision 只有在完整全局唯一证明时可审查。"
        )
        self._update_buttons()


def _repo_result_message(result: object) -> str:
    target = getattr(result, "target", "repo")
    freed = getattr(result, "vendor_freed", "unknown")
    return f"已通过 hf cache rm 删除 {target}; vendor reported freed={freed}。正在刷新。"


def _revision_result_message(result: object) -> str:
    target = getattr(result, "target", "revision")
    freed = getattr(result, "vendor_freed", "unknown")
    return f"已通过 hf cache rm 删除 revision {target}; vendor reported freed={freed}。正在刷新。"


def _prune_result_message(result: object) -> str:
    revisions = getattr(result, "revisions_deleted", 0)
    incomplete = getattr(result, "incomplete_deleted", 0)
    freed = getattr(result, "vendor_freed", "unknown")
    return (
        f"hf cache prune 完成：{revisions} detached revisions / {incomplete} incomplete downloads; "
        f"vendor reported freed={freed}。正在刷新。"
    )


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value:,} B"


def open_huggingface_hub_maintenance_dialog(parent: tk.Tk | tk.Toplevel) -> None:
    _HuggingFaceHubMaintenanceDialog(parent).show()


__all__ = ["open_huggingface_hub_maintenance_dialog"]
