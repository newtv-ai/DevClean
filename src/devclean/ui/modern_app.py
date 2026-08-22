"""Task-oriented desktop shell for DevClean.

The safety and mutation workflow stays in :mod:`devclean.ui.app`. This module
changes presentation and the default scan plan: the home screen starts in an
actionable smart scan that visits source-audited whole-tree cleanup roots
instead of walking the entire user profile. A deep scan remains one click away.
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
from typing import Any
from uuid import uuid4

from devclean.core.application_cleanup import DecisionOwner
from devclean.core.cleanup_catalog import (
    CleanupPolicy,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import RuleConfigError, UserRules, load_rules
from devclean.platform.windows.volumes import fixed_volume_roots
from devclean.scanner import CancellationToken
from devclean.ui import app

# Neutral Windows-style palette. Semantic colors are reserved for meaning:
# blue = primary action, green = safe cleanup, amber = user review, red = danger.
_BG = "#F5F7FA"
_SURFACE = "#FFFFFF"
_SURFACE_ALT = "#F8FAFC"
_CONTROL = "#F3F4F6"
_CONTROL_HOVER = "#E8EEF8"
_BORDER = "#E5E7EB"
_BORDER_STRONG = "#D1D5DB"
_TEXT = "#1F2937"
_MUTED = "#6B7280"
_FAINT = "#9CA3AF"
_PRIMARY = "#2563EB"
_PRIMARY_ACTIVE = "#1D4ED8"
_PRIMARY_SOFT = "#EAF1FF"
_SAFE = "#16A34A"
_SAFE_ACTIVE = "#15803D"
_SAFE_SOFT = "#ECFDF3"
_REVIEW = "#D97706"
_REVIEW_SOFT = "#FFF7E8"
_DANGER = "#DC2626"
_DANGER_ACTIVE = "#B91C1C"
_DISABLED = "#C7CDD6"


def _is_actionable_whole_tree_root(root: KnownCleanupRoot) -> bool:
    """Return whether the root already carries exact audited delete authority."""

    rule = root.application_rule
    return (
        root.delete_root_itself
        and root.policy is CleanupPolicy.VENDOR_MANAGED
        and rule is not None
        and rule.owner is DecisionOwner.TOOL
        and rule.allow_whole_tree
    )


def ordered_drive_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    """Return drive roots in the natural Windows order: C:, D:, E: ..."""

    return tuple(sorted(roots, key=lambda root: str(root).casefold()))


def smart_scan_targets(
    known_roots: Sequence[KnownCleanupRoot],
    drives: Sequence[Path],
    rules: UserRules,
) -> tuple[Path, ...]:
    """Plan the fast home scan without widening cleanup authority.

    Smart mode deliberately excludes the broad USERPROFILE traversal and
    report-only inventory roots. Explicit user additional paths still flow
    through the original target planner. Deep mode remains unchanged.
    """

    actionable = tuple(
        root for root in known_roots if _is_actionable_whole_tree_root(root)
    )
    smart_rules = UserRules(
        scan=replace(rules.scan, include_user_profile=False),
        delete=rules.delete,
        keep=rules.keep,
    )
    return app.scan_targets(actionable, drives, smart_rules)


class ModernDevCleanWindow(app.DevCleanWindow):
    """Modern shell around the existing fail-closed scan/review/cleanup engine."""

    def __init__(self, root: tk.Tk) -> None:
        self._scan_mode = tk.StringVar(master=root, value="smart")
        self._mode_hint = tk.StringVar(master=root)
        super().__init__(root)
        root.geometry("1280x800")
        root.minsize(1060, 680)
        self._sync_mode_hint()

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
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "SectionOnPage.TLabel",
            background=_BG,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 13),
        )
        style.configure(
            "Body.TLabel",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "BodyOnPage.TLabel",
            background=_BG,
            foreground=_MUTED,
            font=("Segoe UI", 9),
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
            foreground=_TEXT,
            font=("Segoe UI Semibold", 24),
        )
        style.configure(
            "MetricSafe.TLabel",
            background=_SURFACE,
            foreground=_SAFE,
            font=("Segoe UI Semibold", 24),
        )
        style.configure(
            "MetricReview.TLabel",
            background=_SURFACE,
            foreground=_REVIEW,
            font=("Segoe UI Semibold", 24),
        )
        style.configure(
            "ModeHint.TLabel",
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
        self._button_style(style, "Danger", _DANGER, _DANGER_ACTIVE)

        style.configure(
            "Modern.Treeview",
            background=_SURFACE,
            fieldbackground=_SURFACE,
            foreground=_TEXT,
            borderwidth=0,
            rowheight=31,
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
        style.map(
            "Modern.Treeview.Heading",
            background=[("active", "#EEF2F7")],
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
            text="开发环境存储清理 · 只删除有明确依据的项目",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        header_actions = ttk.Frame(header, style="App.TFrame")
        header_actions.grid(row=0, column=1, sticky="e")
        ttk.Button(
            header_actions,
            text="工具中心",
            style="Quiet.TButton",
            command=self._open_tools,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._rule_button = ttk.Button(
            header_actions,
            text="规则设置",
            style="Quiet.TButton",
            command=self._edit_rules,
        )
        self._rule_button.pack(side=tk.LEFT)

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
                padx=12,
                pady=6,
            )
            chip.pack(side=tk.LEFT, padx=(0, 7))

        mode_box = ttk.Frame(panel, style="Surface.TFrame")
        mode_box.grid(row=0, column=1, sticky="w", padx=(28, 26))
        ttk.Label(mode_box, text="扫描方式", style="Section.TLabel").pack(anchor=tk.W)
        mode_row = ttk.Frame(mode_box, style="Surface.TFrame")
        mode_row.pack(anchor=tk.W, pady=(8, 0))
        for value, label in (("smart", "智能扫描"), ("deep", "深度扫描")):
            radio = tk.Radiobutton(
                mode_row,
                text=label,
                variable=self._scan_mode,
                value=value,
                command=self._sync_mode_hint,
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
                padx=12,
                pady=6,
            )
            radio.pack(side=tk.LEFT, padx=(0, 7))

        action_box = ttk.Frame(panel, style="Surface.TFrame")
        action_box.grid(row=0, column=2, sticky="e")
        ttk.Label(action_box, text="开始", style="Section.TLabel").pack(anchor=tk.E)
        self._rescan = ttk.Button(
            action_box,
            text="开始扫描",
            style="Primary.TButton",
            command=self._start_scan,
        )
        self._rescan.pack(anchor=tk.E, pady=(8, 0))

        ttk.Label(
            panel,
            textvariable=self._mode_hint,
            style="ModeHint.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(11, 0))

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
        result_title = ttk.Frame(page, style="App.TFrame")
        result_title.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            result_title,
            text="扫描结果",
            style="SectionOnPage.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            result_title,
            text="结果会随扫描持续更新；双击路径可在资源管理器中确认。",
            style="BodyOnPage.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0), pady=(3, 0))

        buckets = ttk.Frame(page, style="App.TFrame")
        buckets.pack(fill=tk.BOTH, expand=True)
        buckets.columnconfigure(0, weight=11, uniform="results")
        buckets.columnconfigure(1, weight=9, uniform="results")
        buckets.rowconfigure(0, weight=1)

        self._deletable_tree = self._build_bucket(
            buckets,
            column=0,
            accent=_SAFE,
            accent_soft=_SAFE_SOFT,
            title="安全可清理",
            hint="已有明确本地证据，并会在执行前再次核验。默认勾选。",
            total=self._deletable_total,
            metric_style="MetricSafe.TLabel",
            buttons=(
                ("all", "全选", "Quiet", lambda: self._check_all(True)),
                ("none", "清空选择", "Quiet", lambda: self._check_all(False)),
                (
                    "recycle",
                    "移到回收站",
                    "Safe",
                    lambda: self._delete(irreversible=False),
                ),
                (
                    "purge",
                    "彻底删除",
                    "Danger",
                    lambda: self._delete(irreversible=True),
                ),
            ),
            checkable=True,
        )
        self._unsure_tree = self._build_bucket(
            buckets,
            column=1,
            accent=_REVIEW,
            accent_soft=_REVIEW_SOFT,
            title="需要你决定",
            hint="技术含义明确，但保留与否取决于你的用途；拿不准可交给 AI 辅助判断。",
            total=self._unsure_total,
            metric_style="MetricReview.TLabel",
            buttons=(
                ("export", "交给 AI", "Quiet", self._export_for_ai),
                ("import", "导入结果", "Quiet", self._import_from_ai),
                ("decide", "我来决定", "Quiet", self._decide_ai_unsure),
                ("forget", "清空判决", "Quiet", self._forget_verdicts),
            ),
        )
        self._unsure_tree.bind(
            "<<TreeviewSelect>>",
            lambda _event: self._sync_buttons(),
        )

    def _open_tools(self) -> None:
        self._root.event_generate("<<DevCleanOpenTools>>", when="tail")

    def _build_bucket(
        self,
        parent: ttk.Frame,
        *,
        column: int,
        accent: str,
        accent_soft: str,
        title: str,
        hint: str,
        total: tk.StringVar,
        metric_style: str,
        buttons: tuple[tuple[str, str, str, Any], ...],
        checkable: bool = False,
    ) -> ttk.Treeview:
        shell = self._card(parent, padx=0, pady=0)
        shell.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0, 7) if column == 0 else (7, 0),
        )
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        head = tk.Frame(shell, background=_SURFACE, padx=16, pady=14)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)

        title_line = tk.Frame(head, background=_SURFACE)
        title_line.grid(row=0, column=0, sticky="w")
        badge = tk.Label(
            title_line,
            text="●",
            background=accent_soft,
            foreground=accent,
            font=("Segoe UI", 9),
            padx=7,
            pady=2,
        )
        badge.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(
            title_line,
            text=title,
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 11),
        ).pack(side=tk.LEFT)

        ttk.Label(
            head,
            textvariable=total,
            style=metric_style,
        ).grid(row=1, column=0, sticky="w", pady=(8, 2))
        tk.Label(
            head,
            text=hint,
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
            anchor="w",
            justify=tk.LEFT,
            wraplength=520,
        ).grid(row=2, column=0, sticky="ew")

        separator = tk.Frame(shell, background=_BORDER, height=1)
        separator.grid(row=0, column=0, sticky="sew")

        holder = ttk.Frame(
            shell,
            style="Surface.TFrame",
            padding=(14, 2, 14, 0),
        )
        holder.grid(row=1, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder,
            columns=("check", "size", "path"),
            show="headings",
            style="Modern.Treeview",
            height=15,
        )
        tree.heading("check", text="", anchor=tk.CENTER)
        tree.heading("size", text="大小", anchor=tk.E)
        tree.heading("path", text="位置", anchor=tk.W)
        tree.column("check", width=38, anchor=tk.CENTER, stretch=False)
        tree.column("size", width=96, anchor=tk.E, stretch=False)
        tree.column(
            "path",
            width=440,
            minwidth=220,
            anchor=tk.W,
            stretch=True,
        )
        tree.tag_configure("odd", background="#FAFBFC")
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vertical.set)
        vertical.grid(row=0, column=1, sticky="ns")
        if checkable:
            tree.bind("<Button-1>", self._on_row_click)
        tree.bind("<Double-1>", self._on_row_double_click)

        actions = ttk.Frame(
            shell,
            style="Surface.TFrame",
            padding=(14, 12, 6, 14),
        )
        actions.grid(row=2, column=0, sticky="ew")
        for key, label, kind, command in buttons:
            button = ttk.Button(
                actions,
                text=label,
                style=f"{kind}.TButton",
                command=command,
            )
            button.pack(side=tk.LEFT, padx=(0, 8))
            self._buttons[key] = button
        return tree

    def _sync_mode_hint(self) -> None:
        if self._scan_mode.get() == "deep":
            self._mode_hint.set(
                "深度扫描会遍历用户目录并做完整分类，适合排查；文件很多时会明显更慢。"
            )
        else:
            self._mode_hint.set(
                "推荐：智能扫描只检查已有明确清理依据的位置，速度更快，也更适合日常使用。"
            )

    def _start_scan(self) -> None:
        try:
            self._rules = load_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror(
                "规则文件有误",
                f"{error}\n\n请点击“规则设置”修正后再扫描。",
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
        self._sync_buttons()
        self._progress.start(60)

        mode = self._scan_mode.get()
        preparing = "正在准备智能扫描…" if mode == "smart" else "正在准备深度扫描…"
        self._status.set(preparing)
        self._scan_rules = self._rules

        # Root discovery can call installed vendor CLIs. The GUI entry point
        # suppresses console allocation for those console child processes.
        self._known_roots = discover_known_cleanup_roots(self._scan_rules.scan)
        roots = (
            smart_scan_targets(self._known_roots, drives, self._scan_rules)
            if mode == "smart"
            else app.scan_targets(self._known_roots, drives, self._scan_rules)
        )
        if not roots:
            self._progress.stop()
            self._busy = None
            self._sync_buttons()
            self._status.set("所选磁盘上没有符合当前扫描模式的可清理位置。")
            return

        mode_name = "智能" if mode == "smart" else "深度"
        self._status.set(
            f"正在{mode_name}扫描… 已规划 {len(roots):,} 个扫描根。"
        )
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


__all__ = ["ModernDevCleanWindow", "ordered_drive_roots", "smart_scan_targets"]
