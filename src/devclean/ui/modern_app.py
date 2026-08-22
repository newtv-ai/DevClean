"""Warm, task-oriented desktop shell for DevClean.

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
from devclean.scanner import CancellationToken
from devclean.ui import app

_BG = "#F6F1EA"
_SURFACE = "#FFFDFC"
_SOFT = "#F1E9DE"
_BORDER = "#E4D9CC"
_TEXT = "#26342F"
_MUTED = "#6F756F"
_PRIMARY = "#B85F43"
_PRIMARY_ACTIVE = "#A95037"
_SAFE = "#477665"
_SAFE_ACTIVE = "#396354"
_REVIEW = "#B77B35"
_DANGER = "#A94F45"
_DISABLED = "#C9C2B9"


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
        root.geometry("1280x780")
        root.minsize(1040, 660)
        self._sync_mode_hint()

    def _configure_style(self) -> None:
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self._root.configure(background=_BG)

        style.configure("Modern.TFrame", background=_BG)
        style.configure("Surface.TFrame", background=_SURFACE)
        style.configure("Soft.TFrame", background=_SOFT)
        style.configure(
            "Hero.TLabel",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 23),
        )
        style.configure(
            "HeroSub.TLabel",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Eyebrow.TLabel",
            background=_SURFACE,
            foreground=_PRIMARY,
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "StatusWarm.TLabel",
            background=_SOFT,
            foreground=_TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "ModeHint.TLabel",
            background=_SOFT,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "CardTitleWarm.TLabel",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 12),
        )
        style.configure(
            "AmountWarm.TLabel",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 25),
        )
        style.configure(
            "HintWarm.TLabel",
            background=_SURFACE,
            foreground=_MUTED,
            font=("Segoe UI", 9),
        )
        style.configure(
            "DriveWarm.TCheckbutton",
            background=_SURFACE,
            foreground=_TEXT,
            font=("Segoe UI", 10),
        )
        style.map(
            "DriveWarm.TCheckbutton",
            background=[("active", _SURFACE)],
            foreground=[("active", _TEXT)],
        )
        style.configure(
            "Mode.TRadiobutton",
            background=_SOFT,
            foreground=_TEXT,
            font=("Segoe UI Semibold", 9),
            padding=(8, 5),
        )
        style.map(
            "Mode.TRadiobutton",
            background=[("active", _SOFT)],
            foreground=[("active", _PRIMARY)],
        )

        self._button_style(style, "Primary", _PRIMARY, _PRIMARY_ACTIVE)
        self._button_style(style, "Safe", _SAFE, _SAFE_ACTIVE)
        self._button_style(style, "Quiet", _SOFT, "#E8DED1", foreground=_TEXT)
        self._button_style(style, "Danger", _DANGER, "#93443C")

        style.configure(
            "Warm.Treeview",
            background=_SURFACE,
            fieldbackground=_SURFACE,
            foreground=_TEXT,
            borderwidth=0,
            rowheight=30,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Warm.Treeview.Heading",
            background=_SOFT,
            foreground=_MUTED,
            relief=tk.FLAT,
            borderwidth=0,
            padding=(9, 7),
            font=("Segoe UI Semibold", 9),
        )
        style.map("Warm.Treeview.Heading", background=[("active", "#E9DFD3")])
        style.layout("Warm.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure(
            "Warm.Horizontal.TProgressbar",
            background=_PRIMARY,
            troughcolor=_SOFT,
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
            padding=(14, 9),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            f"{name}.TButton",
            background=[("active", active), ("disabled", _DISABLED)],
            foreground=[("disabled", "#F4F1ED")],
        )

    def _build(self) -> None:
        self._configure_style()

        page = ttk.Frame(
            self._root,
            style="Modern.TFrame",
            padding=(22, 18, 22, 20),
        )
        page.pack(fill=tk.BOTH, expand=True)

        hero = tk.Frame(
            page,
            background=_SURFACE,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=22,
            pady=18,
        )
        hero.pack(fill=tk.X)
        hero.columnconfigure(0, weight=1)

        title_box = ttk.Frame(hero, style="Surface.TFrame")
        title_box.grid(row=0, column=0, rowspan=2, sticky="w")
        ttk.Label(
            title_box,
            text="DEV STORAGE CARE",
            style="Eyebrow.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(title_box, text="DevClean", style="Hero.TLabel").pack(
            anchor=tk.W,
            pady=(1, 2),
        )
        ttk.Label(
            title_box,
            text=(
                "把真正有依据的缓存清理出来，把需要厂商工具或你本人决定的内容"
                "留在正确的位置。"
            ),
            style="HeroSub.TLabel",
        ).pack(anchor=tk.W)

        actions = ttk.Frame(hero, style="Surface.TFrame")
        actions.grid(row=0, column=1, sticky="e")
        ttk.Button(
            actions,
            text="工具中心",
            style="Quiet.TButton",
            command=self._open_tools,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._rule_button = ttk.Button(
            actions,
            text="规则设置",
            style="Quiet.TButton",
            command=self._edit_rules,
        )
        self._rule_button.pack(side=tk.LEFT, padx=(0, 8))
        self._rescan = ttk.Button(
            actions,
            text="开始扫描",
            style="Primary.TButton",
            command=self._start_scan,
        )
        self._rescan.pack(side=tk.LEFT)

        drives = ttk.Frame(hero, style="Surface.TFrame")
        drives.grid(row=1, column=1, sticky="e", pady=(10, 0))
        ttk.Label(
            drives,
            text="扫描盘符",
            style="HeroSub.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 7))
        preferred = app._system_drive()
        for drive in reversed(app.fixed_volume_roots()):
            state = tk.BooleanVar(value=drive == preferred)
            self._drive_vars[drive] = state
            ttk.Checkbutton(
                drives,
                text=str(drive)[:2],
                variable=state,
                style="DriveWarm.TCheckbutton",
            ).pack(side=tk.LEFT, padx=(4, 0))

        scan_bar = tk.Frame(
            page,
            background=_SOFT,
            highlightbackground=_BORDER,
            highlightthickness=1,
            padx=16,
            pady=10,
        )
        scan_bar.pack(fill=tk.X, pady=(12, 0))
        mode_box = ttk.Frame(scan_bar, style="Soft.TFrame")
        mode_box.pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_box,
            text="智能扫描",
            variable=self._scan_mode,
            value="smart",
            style="Mode.TRadiobutton",
            command=self._sync_mode_hint,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_box,
            text="深度扫描",
            variable=self._scan_mode,
            value="deep",
            style="Mode.TRadiobutton",
            command=self._sync_mode_hint,
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Label(
            scan_bar,
            textvariable=self._mode_hint,
            style="ModeHint.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._progress = ttk.Progressbar(
            page,
            style="Warm.Horizontal.TProgressbar",
            mode="indeterminate",
        )
        self._progress.pack(fill=tk.X, pady=(10, 0))

        status_card = ttk.Frame(page, style="Soft.TFrame", padding=(14, 9))
        status_card.pack(fill=tk.X, pady=(6, 12))
        ttk.Label(
            status_card,
            textvariable=self._status,
            style="StatusWarm.TLabel",
        ).pack(anchor=tk.W)

        buckets = ttk.Frame(page, style="Modern.TFrame")
        buckets.pack(fill=tk.BOTH, expand=True)
        buckets.columnconfigure(0, weight=11, uniform="results")
        buckets.columnconfigure(1, weight=9, uniform="results")
        buckets.rowconfigure(0, weight=1)

        self._deletable_tree = self._build_bucket(
            buckets,
            column=0,
            accent=_SAFE,
            title="安全可清理",
            hint=(
                "这里只显示已经有明确本地证据、并且仍会在执行前再次核验的项目。"
                "默认勾选；双击路径可先去资源管理器确认。"
            ),
            total=self._deletable_total,
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
            title="需要你决定",
            hint=(
                "这里不是“疑似垃圾箱”。只有技术含义明确、但保留与否取决于你的用途"
                "时才会出现；拿不准时可以把选中的文件交给 AI 辅助判断。"
            ),
            total=self._unsure_total,
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
        title: str,
        hint: str,
        total: tk.StringVar,
        buttons: tuple[tuple[str, str, str, Any], ...],
        checkable: bool = False,
    ) -> ttk.Treeview:
        shell = tk.Frame(
            parent,
            background=_SURFACE,
            highlightbackground=_BORDER,
            highlightthickness=1,
        )
        shell.grid(
            row=0,
            column=column,
            sticky="nsew",
            padx=(0, 7) if column == 0 else (7, 0),
        )
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        head = ttk.Frame(
            shell,
            style="Surface.TFrame",
            padding=(16, 15, 16, 10),
        )
        head.grid(row=0, column=0, sticky="ew")
        title_row = ttk.Frame(head, style="Surface.TFrame")
        title_row.pack(fill=tk.X)
        tk.Label(
            title_row,
            text="●",
            background=_SURFACE,
            foreground=accent,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            title_row,
            text=title,
            style="CardTitleWarm.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            head,
            textvariable=total,
            style="AmountWarm.TLabel",
        ).pack(anchor=tk.W, pady=(7, 2))
        ttk.Label(
            head,
            text=hint,
            style="HintWarm.TLabel",
            wraplength=520,
        ).pack(anchor=tk.W)

        holder = ttk.Frame(
            shell,
            style="Surface.TFrame",
            padding=(16, 2, 16, 0),
        )
        holder.grid(row=1, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder,
            columns=("check", "size", "path"),
            show="headings",
            style="Warm.Treeview",
            height=15,
        )
        tree.heading("check", text="", anchor=tk.CENTER)
        tree.heading("size", text="大小", anchor=tk.E)
        tree.heading("path", text="位置", anchor=tk.W)
        tree.column("check", width=36, anchor=tk.CENTER, stretch=False)
        tree.column("size", width=94, anchor=tk.E, stretch=False)
        tree.column(
            "path",
            width=420,
            minwidth=220,
            anchor=tk.W,
            stretch=True,
        )
        tree.tag_configure("odd", background="#FBF7F2")
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
            padding=(16, 12, 8, 15),
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
                "遍历用户目录并做完整分类；适合排查，文件很多时会明显更慢。"
            )
        else:
            self._mode_hint.set(
                "推荐：只扫已有明确清理依据的位置；更快，也更符合主页“可清理”结果。"
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
            messagebox.showinfo("DevClean", "请先勾选至少一个盘符。")
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
            self._status.set("所选盘符上没有符合当前扫描模式的可清理位置。")
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


__all__ = ["ModernDevCleanWindow", "smart_scan_targets"]
