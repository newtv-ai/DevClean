"""One-click desktop shell for DevClean.

The scanner and mutation safety boundary stay in :mod:`devclean.ui.app`. This
module deliberately keeps the normal workflow small: choose drives, scan, then
clean the audited regenerable caches. Advanced/user-owned storage remains out
of the automatic path instead of becoming another list the user must manage.
"""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from tkinter import messagebox, ttk
from uuid import uuid4

from devclean.core.application_cleanup import (
    DecisionOwner,
    RebuildCost,
    application_process_running,
    clear_process_cache,
)
from devclean.core.cleanup_catalog import (
    CleanupPolicy,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.cleanup_journal import ActionState
from devclean.core.postscan_cleanup import CleanupExecutionResult
from devclean.core.triage import TriageSession
from devclean.core.user_rules import RuleConfigError, UserRules, load_rules
from devclean.platform.windows.volumes import fixed_volume_roots
from devclean.scanner import CancellationToken
from devclean.ui import app

_BG = "#F5F7FA"
_SURFACE = "#FFFFFF"
_SURFACE_ALT = "#F8FAFC"
_CONTROL = "#F3F4F6"
_CONTROL_HOVER = "#E8EEF8"
_BORDER = "#E5E7EB"
_BORDER_STRONG = "#D1D5DB"
_TEXT = "#1F2937"
_MUTED = "#6B7280"
_PRIMARY = "#2563EB"
_PRIMARY_ACTIVE = "#1D4ED8"
_PRIMARY_SOFT = "#EAF1FF"
_SAFE = "#16A34A"
_SAFE_ACTIVE = "#15803D"
_DANGER = "#DC2626"
_DISABLED = "#C7CDD6"


def _is_actionable_whole_tree_root(root: KnownCleanupRoot) -> bool:
    """Return whether the root has audited whole-tree delete authority."""

    rule = root.application_rule
    return (
        root.delete_root_itself
        and root.policy is CleanupPolicy.VENDOR_MANAGED
        and rule is not None
        and rule.owner is DecisionOwner.TOOL
        and rule.allow_whole_tree
        # Expensive indexes/models are not one-click junk. Deep/advanced tools
        # may still expose them, but the normal cleaner must not create a large
        # rebuild just to make its reclaimed-space number look bigger.
        and rule.rebuild_cost is not RebuildCost.HIGH
    )


def ordered_drive_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    """Return drive roots in the natural Windows order: C:, D:, E: ..."""

    return tuple(sorted(roots, key=lambda root: str(root).casefold()))


def automatic_cleanup_roots(
    known_roots: Sequence[KnownCleanupRoot],
) -> tuple[KnownCleanupRoot, ...]:
    """Return audited roots that can actually be cleaned in the current session.

    A cache whose owning application must be closed is not advertised as
    "安全可清理" while that application is running. This prevents the old UX in
    which dozens of visible rows were guaranteed to fail during cleanup.
    """

    clear_process_cache()
    running_by_app: dict[str, bool] = {}
    ready: list[KnownCleanupRoot] = []
    for root in known_roots:
        if not _is_actionable_whole_tree_root(root):
            continue
        rule = root.application_rule
        assert rule is not None
        if rule.requires_process_closed:
            running = running_by_app.get(rule.app_id)
            if running is None:
                running = application_process_running(rule.app_id)
                running_by_app[rule.app_id] = running
            if running:
                continue
        ready.append(root)
    return tuple(ready)


def smart_scan_targets(
    known_roots: Sequence[KnownCleanupRoot],
    drives: Sequence[Path],
    rules: UserRules,
) -> tuple[Path, ...]:
    """Plan the normal fast scan without widening cleanup authority."""

    smart_rules = UserRules(
        scan=replace(rules.scan, include_user_profile=False),
        delete=rules.delete,
        keep=rules.keep,
    )
    return app.scan_targets(automatic_cleanup_roots(known_roots), drives, smart_rules)


class ModernDevCleanWindow(app.DevCleanWindow):
    """Small one-click shell around the fail-closed cleanup engine."""

    def __init__(self, root: tk.Tk) -> None:
        self._scan_mode = tk.StringVar(master=root, value="smart")
        self._modern_scan_in_progress = False
        super().__init__(root)
        root.geometry("1180x760")
        root.minsize(960, 620)

    def _configure_style(self) -> None:
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self._root.configure(background=_BG)

        style.configure("App.TFrame", background=_BG)
        style.configure("Surface.TFrame", background=_SURFACE)
        style.configure("Alt.TFrame", background=_SURFACE_ALT)
        style.configure(
            "Title.TLabel",
            background=_BG,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 22),
        )
        style.configure(
            "Subtitle.TLabel",
            background=_BG,
            foreground=_MUTED,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Section.TLabel",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "Status.TLabel",
            background=_SURFACE_ALT,
            foreground=_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Metric.TLabel",
            background=_SURFACE,
            foreground=_SAFE,
            font=("Segoe UI Semibold", 26),
        )
        style.configure(
            "Body.TLabel",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        )

        self._button_style(style, "Primary", _PRIMARY, _PRIMARY_ACTIVE)
        self._button_style(style, "Safe", _SAFE, _SAFE_ACTIVE)
        self._button_style(
            style,
            "Quiet",
            _CONTROL,
            _CONTROL_HOVER,
            foreground=_TEXT,
        )
        self._button_style(style, "Danger", _DANGER, "#B91C1C")

        style.configure(
            "Modern.Treeview",
            background=_SURFACE,
            fieldbackground=_SURFACE,
            foreground=_TEXT,
            borderwidth=0,
            rowheight=32,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=_SURFACE_ALT,
            foreground=_MUTED,
            relief=tk.FLAT,
            borderwidth=0,
            padding=(10, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.layout("Modern.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure(
            "Modern.Horizontal.TProgressbar",
            background=_PRIMARY,
            troughcolor="#E8ECF2",
            borderwidth=0,
            thickness=4,
        )

    @staticmethod
    def _button_style(
        style: ttk.Style,
        name: str,
        background: str,
        active: str,
        *,
        foreground: str = "#FFFFFF",
    ) -> None:
        style.configure(
            f"{name}.TButton",
            background=background,
            foreground=foreground,
            borderwidth=0,
            padding=(15, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            f"{name}.TButton",
            background=[("active", active), ("disabled", _DISABLED)],
            foreground=[("disabled", "#F5F7FA")],
        )

    @staticmethod
    def _card(parent: tk.Misc, *, padx: int = 18, pady: int = 16) -> tk.Frame:
        return tk.Frame(
            parent,
            background=_SURFACE,
            highlightbackground=_BORDER,
            highlightthickness=1,
            borderwidth=0,
            padx=padx,
            pady=pady,
        )

    def _build(self) -> None:
        self._configure_style()
        page = ttk.Frame(
            self._root,
            style="App.TFrame",
            padding=(24, 20, 24, 22),
        )
        page.pack(fill=tk.BOTH, expand=True)

        self._build_header(page)
        self._build_scan_controls(page)
        self._build_status(page)
        self._build_results(page)

    def _build_header(self, page: ttk.Frame) -> None:
        header = ttk.Frame(page, style="App.TFrame")
        header.pack(fill=tk.X, pady=(0, 14))
        header.columnconfigure(0, weight=1)

        brand = ttk.Frame(header, style="App.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="DevClean", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            brand,
            text="扫描可再生成的缓存与开发产物，然后一键释放空间",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        self._rule_button = ttk.Button(
            header,
            text="设置",
            style="Quiet.TButton",
            command=self._edit_rules,
        )
        self._rule_button.grid(row=0, column=1, sticky="e")

    def _build_scan_controls(self, page: ttk.Frame) -> None:
        panel = self._card(page, padx=18, pady=15)
        panel.pack(fill=tk.X)
        panel.columnconfigure(0, weight=1)

        range_box = ttk.Frame(panel, style="Surface.TFrame")
        range_box.grid(row=0, column=0, sticky="w")
        ttk.Label(range_box, text="扫描磁盘", style="Section.TLabel").pack(anchor=tk.W)
        drive_row = ttk.Frame(range_box, style="Surface.TFrame")
        drive_row.pack(anchor=tk.W, pady=(8, 0))
        preferred = app._system_drive()
        for drive in ordered_drive_roots(fixed_volume_roots()):
            state = tk.BooleanVar(value=drive == preferred)
            self._drive_vars[drive] = state
            chip = tk.Checkbutton(
                drive_row,
                text=str(drive)[:2],
                variable=state,
                indicatoron=False,
                relief=tk.FLAT,
                offrelief=tk.FLAT,
                overrelief=tk.FLAT,
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=_BORDER_STRONG,
                highlightcolor=_PRIMARY,
                background=_CONTROL,
                activebackground=_CONTROL_HOVER,
                selectcolor=_PRIMARY_SOFT,
                foreground=_TEXT,
                activeforeground=_PRIMARY,
                cursor="hand2",
                font=("Segoe UI Semibold", 9),
                padx=13,
                pady=6,
            )
            chip.pack(side=tk.LEFT, padx=(0, 7))

        action_box = ttk.Frame(panel, style="Surface.TFrame")
        action_box.grid(row=0, column=1, sticky="e")
        self._rescan = ttk.Button(
            action_box,
            text="扫描",
            style="Primary.TButton",
            command=self._start_scan,
        )
        self._rescan.pack(anchor=tk.E)

        ttk.Label(
            panel,
            text=(
                "自动检查浏览器、IDE、AI 工具和包管理器的可再生成缓存；"
                "正在运行的应用和高重建成本内容会自动跳过。"
            ),
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(11, 0))

    def _build_status(self, page: ttk.Frame) -> None:
        self._progress = ttk.Progressbar(
            page,
            style="Modern.Horizontal.TProgressbar",
            mode="indeterminate",
        )
        self._progress.pack(fill=tk.X, pady=(11, 0))
        status_shell = ttk.Frame(page, style="Alt.TFrame", padding=(12, 8))
        status_shell.pack(fill=tk.X, pady=(5, 14))
        ttk.Label(
            status_shell,
            textvariable=self._status,
            style="Status.TLabel",
        ).pack(anchor=tk.W)

    def _build_results(self, page: ttk.Frame) -> None:
        shell = self._card(page, padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True)
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        head = tk.Frame(shell, background=_SURFACE, padx=18, pady=15)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        tk.Label(
            head,
            text="可清理空间",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 12),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            head,
            textvariable=self._deletable_total,
            style="Metric.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(5, 1))
        tk.Label(
            head,
            text="默认全部选择。这里仅显示当前可以直接清理的可再生成内容。",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        ).grid(row=2, column=0, sticky="w")

        holder = ttk.Frame(shell, style="Surface.TFrame", padding=(14, 0, 14, 0))
        holder.grid(row=1, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self._deletable_tree = ttk.Treeview(
            holder,
            columns=("check", "size", "path"),
            show="headings",
            style="Modern.Treeview",
            height=16,
        )
        self._deletable_tree.heading("check", text="", anchor=tk.CENTER)
        self._deletable_tree.heading("size", text="大小", anchor=tk.E)
        self._deletable_tree.heading("path", text="位置", anchor=tk.W)
        self._deletable_tree.column("check", width=40, anchor=tk.CENTER, stretch=False)
        self._deletable_tree.column("size", width=105, anchor=tk.E, stretch=False)
        self._deletable_tree.column(
            "path",
            width=760,
            minwidth=360,
            anchor=tk.W,
            stretch=True,
        )
        self._deletable_tree.tag_configure("odd", background="#FAFBFC")
        self._deletable_tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(
            holder,
            orient=tk.VERTICAL,
            command=self._deletable_tree.yview,
        )
        self._deletable_tree.configure(yscrollcommand=vertical.set)
        vertical.grid(row=0, column=1, sticky="ns")
        self._deletable_tree.bind("<Button-1>", self._on_row_click)
        self._deletable_tree.bind("<Double-1>", self._on_row_double_click)

        # The core still tracks ambiguous items for fail-closed classification,
        # but the normal cleaner does not ask the user to become a cache expert.
        # Keep a non-rendered tree so the inherited event pipeline remains the
        # single source of truth; ambiguous rows are simply protected.
        self._unsure_tree = ttk.Treeview(
            page,
            columns=("check", "size", "path"),
            show="headings",
        )

        actions = ttk.Frame(shell, style="Surface.TFrame", padding=(14, 12, 14, 14))
        actions.grid(row=2, column=0, sticky="ew")
        self._buttons["all"] = ttk.Button(
            actions,
            text="全选",
            style="Quiet.TButton",
            command=lambda: self._check_all(True),
        )
        self._buttons["all"].pack(side=tk.LEFT, padx=(0, 8))
        self._buttons["none"] = ttk.Button(
            actions,
            text="取消选择",
            style="Quiet.TButton",
            command=lambda: self._check_all(False),
        )
        self._buttons["none"].pack(side=tk.LEFT)
        self._buttons["purge"] = ttk.Button(
            actions,
            text="立即清理",
            style="Safe.TButton",
            command=self._confirm_and_clean,
        )
        self._buttons["purge"].pack(side=tk.RIGHT)

        tk.Label(
            actions,
            text="无法自动证明安全的内容会保留，不会要求你逐项判断。",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT, padx=(0, 14))

    def _confirm_and_clean(self) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("DevClean", "没有选择要清理的项目。")
            return
        total = sum(self._size_of(item) for item in items)
        if not messagebox.askyesno(
            "清理缓存",
            f"将永久清理 {len(items):,} 项可再生成内容，预计释放 "
            f"{app._format_bytes(total)}。\n\n继续？",
        ):
            return
        self._delete(irreversible=True)

    def _publish(self, session: TriageSession) -> None:
        super()._publish(session)
        if not self._modern_scan_in_progress:
            return
        self._modern_scan_in_progress = False

        def finish_message() -> None:
            count = len(self._deletable)
            if count:
                self._status.set(
                    f"扫描完成：找到 {count:,} 项当前可直接清理的内容。"
                    "正在使用或不适合自动处理的内容已跳过。"
                )
            else:
                self._status.set(
                    "扫描完成：当前没有可直接清理的内容。"
                    "正在使用的应用缓存和高重建成本内容已自动跳过。"
                )

        self._root.after_idle(finish_message)

    def _report_deletion(
        self,
        results: tuple[CleanupExecutionResult, ...],
        reasons: dict[str, int],
    ) -> None:
        finished = 0
        failed = 0
        freed = 0
        for result in results:
            finished += sum(
                state in {ActionState.PURGED, ActionState.RECYCLED}
                for _action, state in result.action_states
            )
            failed += sum(
                state not in {ActionState.PURGED, ActionState.RECYCLED}
                for _action, state in result.action_states
            )
            freed += result.purged_logical_bytes
        failed += sum(reasons.values())

        parts = [f"已清理 {finished:,} 项"]
        if freed:
            parts.append(f"释放 {app._format_bytes(freed)}")
        if failed:
            parts.append(f"{failed:,} 项因正在变化或不再满足条件而自动跳过")
        self._status.set("；".join(parts) + "。正在自动刷新结果…")

        # Do not leave stale rows behind and tell the user to press Scan again.
        # A cleaner should reconcile its own result automatically.
        self._busy = "refreshing"
        self._sync_buttons()
        self._root.after(350, self._refresh_after_cleanup)

    def _refresh_after_cleanup(self) -> None:
        self._busy = None
        self._start_scan()

    def _start_scan(self) -> None:
        try:
            self._rules = load_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror(
                "规则文件有误",
                f"{error}\n\n请点击“设置”修正后再扫描。",
            )
            return
        drives = tuple(
            drive for drive, state in self._drive_vars.items() if state.get()
        )
        if not drives:
            messagebox.showinfo("DevClean", "请先选择至少一个磁盘。")
            return
        if self._cancel is not None and not self._cancel.is_cancelled():
            self._cancel.cancel()

        self._scan_token = uuid4().hex
        self._scan_session_id = uuid4().hex
        self._cancel = CancellationToken()
        self._deletable.clear()
        self._unsure.clear()
        self._checked.clear()
        self._ai_unsure_reasons.clear()
        self._ai_packages.clear()
        self._ai_group_members.clear()
        self._deletable_tree.delete(*self._deletable_tree.get_children())
        self._unsure_tree.delete(*self._unsure_tree.get_children())
        self._deletable_total.set("—")
        self._unsure_total.set("—")
        self._busy = "scanning"
        self._modern_scan_in_progress = True
        self._sync_buttons()
        self._progress.start(60)
        self._status.set("正在扫描可直接清理的缓存…")
        self._scan_rules = self._rules

        self._known_roots = discover_known_cleanup_roots(self._scan_rules.scan)
        roots = smart_scan_targets(self._known_roots, drives, self._scan_rules)
        if not roots:
            self._progress.stop()
            self._busy = None
            self._modern_scan_in_progress = False
            self._sync_buttons()
            self._status.set(
                "当前没有可直接清理的位置；正在运行的应用和高重建成本内容已自动跳过。"
            )
            return

        self._status.set(f"正在扫描… 已找到 {len(roots):,} 个可清理位置。")
        threading.Thread(
            target=self._scan_worker,
            args=(
                self._scan_token,
                roots,
                self._cancel,
                self._scan_rules,
                self._known_roots,
            ),
            daemon=True,
        ).start()


__all__ = [
    "ModernDevCleanWindow",
    "automatic_cleanup_roots",
    "ordered_drive_roots",
    "smart_scan_targets",
]
