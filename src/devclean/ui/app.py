"""DevClean: scan on launch, sort into two buckets, delete.

Two buckets, because the tool has exactly two answers about any file: it is sure
the file can go, or it is not sure and the question goes to a model.  The
three public rule files define where to scan and what to delete or keep.  There
is no bucket where the user is expected to adjudicate files one by one.

Mutation lives in ``core.postscan_cleanup`` and nothing here can widen it.
"""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import tkinter as tk
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from typing import Any, cast
from uuid import uuid4

from devclean.core import ai_sessions
from devclean.core.ai_review_contract import (
    AiRecommendation,
    AiReviewCandidateInput,
    AiReviewContractError,
    AiReviewPackage,
    build_ai_review_package,
    parse_ai_review_response,
    serialize_ai_review_package,
)
from devclean.core.cleanup_catalog import (
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.cleanup_journal import ActionState, CleanupMode
from devclean.core.paths import data_dir
from devclean.core.postscan_cleanup import (
    CleanupExecutionResult,
    CleanupRefusal,
    ScanCleanupCandidate,
    candidate_from_directory_item,
    candidate_from_triage_item,
    execute_cleanup_batch,
    prepare_cleanup_plan,
)
from devclean.core.triage import (
    Actionability,
    CleanupTargetKind,
    DirectorySubtreeTotals,
    ExecutionPolicy,
    ReviewLane,
    RiskTier,
    TriageItem,
    TriageSession,
    triage_directory,
    triage_file,
)
from devclean.core.user_rules import (
    RuleConfigError,
    RuleDecision,
    UserRules,
    add_ai_verdicts,
    add_user_verdicts,
    clear_ai_rules,
    default_rules,
    expanded_scan_paths,
    load_rules,
    normalise_path,
    read_rule_documents,
)
from devclean.platform.windows.volumes import fixed_volume_roots
from devclean.scanner import (
    CancellationToken,
    ScanOptions,
    ScanRecordKind,
    ScanStats,
    scan_roots,
)
from devclean.ui.rule_editor import RuleEditor, open_rule_editor

_INK = "#1b2942"
_CANVAS = "#f1f4fa"
_SURFACE = "#ffffff"
_GREEN = "#1f9d57"
_AMBER = "#d98a1f"
_RED = "#c0392b"
# Items per export.  Sized so one file stays inside what a model can read in a
# single pass; the biggest items go first because they carry the most value.
_AI_VOLUME_ITEMS = 300
# Rows drawn in the table.  Everything found is kept, selected, exported and
# deleted; this only bounds how many lines tkinter has to lay out, because
# rebuilding tens of thousands of rows every 1.5 seconds during a scan makes
# the window unusable.  The header states the real count next to it.
_ROWS_DRAWN = 2_000

# (path, size in bytes, is a whole directory)
Row = tuple[str, int, bool]


def _system_drive() -> Path | None:
    """Return the drive Windows is on, which is the one that fills up."""

    root = os.environ.get("SYSTEMDRIVE")
    if not root:
        return None
    candidate = Path(f"{root}\\")
    return candidate if candidate in fixed_volume_roots() else None


def _is_vouched_for(item: TriageItem) -> bool:
    """Return whether something other than a guess says this may be removed.

    The review *lane* is the wrong question.  A file inside a catalog-recognised
    vendor cache is classified ``AI_REVIEW`` because asking a model about it is
    *permitted*, not because the tool is unsure -- and reading the lane put
    2.7 GB of pip cache in the "ask the AI" pile while the confident pile held
    165 MB of crash dumps.  What matters is whether the catalog or a
    deterministic rule vouched for it.
    """

    return (
        item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
        or "known_root" in item.tags
        or "whole_directory" in item.tags
    )


def is_direct_cleanup_eligible(item: TriageItem) -> bool:
    """Return whether the tool is confident enough to offer removal."""

    return (
        _is_vouched_for(item)
        and item.actionability in {Actionability.REVIEW_PLAN, Actionability.AI_REVIEW}
        and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        and item.risk_tier is not RiskTier.PROTECTED
    )


def is_ai_review_eligible(item: TriageItem) -> bool:
    """Return whether the tool is unsure and a model should be asked.

    Whole directories stay out: the model answers about files, and a tree is not
    something an adopted single-file recommendation should ever stand for.
    """

    return (
        item.target_kind is CleanupTargetKind.FILE
        and not _is_vouched_for(item)
        and item.lane is ReviewLane.AI_REVIEW
        and item.actionability is Actionability.AI_REVIEW
        and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        and item.risk_tier is not RiskTier.PROTECTED
    )


def _configured_delete_eligible(item: TriageItem) -> bool:
    """Configured DELETE may promote only an item the executor already accepts."""

    return is_direct_cleanup_eligible(item) or is_ai_review_eligible(item)


def _rows_of(
    session: TriageSession, rules: UserRules
) -> tuple[tuple[Row, ...], tuple[Row, ...]]:
    """Render the current buckets as immutable rows, safe to cross threads."""

    def rows(items: tuple[TriageItem, ...]) -> tuple[Row, ...]:
        rendered: list[Row] = []
        for item in items:
            is_directory = item.target_kind is CleanupTargetKind.DIRECTORY
            size = (
                session.subtree_totals(item.path).logical_bytes
                if is_directory
                else item.logical_size
            )
            rendered.append((item.path, size, is_directory))
        rendered.sort(key=lambda row: row[1], reverse=True)
        return tuple(rendered)

    deletable, unsure = _partition_items(session, rules)
    return (rows(deletable), rows(unsure))


def _partition_items(
    session: TriageSession, rules: UserRules
) -> tuple[tuple[TriageItem, ...], tuple[TriageItem, ...]]:
    """Apply the one authoritative KEEP/DELETE/AI partition."""

    deletable: list[TriageItem] = []
    unsure: list[TriageItem] = []
    for item in session.all_items():
        decision = rules.decision_for(item.path)
        if decision is RuleDecision.KEEP:
            continue
        if is_direct_cleanup_eligible(item) or (
            decision is RuleDecision.DELETE
            and _configured_delete_eligible(item)
        ):
            deletable.append(item)
        elif is_ai_review_eligible(item):
            unsure.append(item)
    return (tuple(deletable), tuple(unsure))


def _verdicts_from_session_index(text: str) -> dict[str, tuple[str, str]]:
    """Recover path verdicts from an answer whose export session is long gone."""

    try:
        payload = json.loads(text)
        session = str(payload["review_session_id"])
        # "recommendations" is what the exported instructions ask for; some
        # models answer with "responses" instead, and an answer that names its
        # verdicts differently is still an answer the user paid for.
        responses = payload.get("recommendations", payload.get("responses"))
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}
    known = ai_sessions.recall_export(session)
    if not known or not isinstance(responses, list):
        return {}
    recovered: dict[str, tuple[str, str]] = {}
    allowed = {
        AiRecommendation.KEEP.value,
        AiRecommendation.RECOMMEND_RECYCLE.value,
        AiRecommendation.UNSURE.value,
    }
    for entry in responses:
        if not isinstance(entry, dict):
            continue
        path = known.get(str(entry.get("candidate_id", "")))
        verdict = str(entry.get("recommendation", ""))
        if path and verdict in allowed:
            recovered[path] = (verdict, str(entry.get("reason", ""))[:500])
    return recovered


def _reason_of(error: BaseException) -> str:
    """Name why one object could not be processed, in the user's terms."""

    winerror = getattr(error, "winerror", None)
    if isinstance(error, FileNotFoundError) or winerror in {2, 3}:
        return "扫描后已自行消失"
    if isinstance(error, PermissionError) or winerror in {5, 32, 33}:
        return "被程序占用或权限不足"
    if isinstance(error, CleanupRefusal):
        return "扫描后内容已改变，安全检查拒绝"
    return f"其他（{type(error).__name__}）"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def scan_targets(
    known_roots: Sequence[KnownCleanupRoot],
    drives: Sequence[Path] = (),
    rules: UserRules | None = None,
) -> tuple[Path, ...]:
    """Return the configured profile, catalog, and additional scan roots.

    Defaults cover every catalog root the machine actually has, plus the profile
    so project build output is reachable.  User exclusions and additional roots
    are applied, then the result is reduced to outermost paths so nothing is
    walked twice.  Whole drives are deliberately not walked.
    """

    active_rules = rules or default_rules()
    candidates: list[Path] = []
    profile = os.environ.get("USERPROFILE")
    if profile and active_rules.scan.include_user_profile:
        candidates.append(Path(profile))
    if active_rules.scan.include_known_cleanup_roots:
        candidates.extend(root.path for root in known_roots)
    candidates.extend(expanded_scan_paths(active_rules.scan.additional_paths))
    excluded = tuple(
        normalise_path(path)
        for path in expanded_scan_paths(active_rules.scan.excluded_paths)
    )
    if drives:
        allowed = {str(drive)[:2].casefold() for drive in drives}
        candidates = [
            path for path in candidates if str(path)[:2].casefold() in allowed
        ]
    resolved: list[Path] = []
    for path in candidates:
        normalized_text = normalise_path(path)
        if any(
            normalized_text == blocked
            or normalized_text.startswith(blocked.rstrip(os.sep) + os.sep)
            for blocked in excluded
        ):
            continue
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        normalized = Path(os.path.normcase(os.path.normpath(os.path.abspath(path))))
        if any(normalized.is_relative_to(kept) for kept in resolved):
            continue
        resolved = [kept for kept in resolved if not kept.is_relative_to(normalized)]
        resolved.append(normalized)
    return tuple(resolved)


class DevCleanWindow:
    """The whole product: a scan that starts itself and two lists."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._events: Queue[tuple[str, Any]] = Queue()
        self._known_roots: tuple[KnownCleanupRoot, ...] = ()
        self._session: TriageSession | None = None
        self._cancel: CancellationToken | None = None
        self._scan_token = ""
        self._scan_session_id = uuid4().hex

        self._deletable: list[TriageItem] = []
        self._unsure: list[TriageItem] = []
        # Paths whose checkbox is ticked.  Only these are ever deleted.
        self._checked: set[str] = set()
        # Every package exported this session, so an answer can be imported
        # whichever export it came from.
        self._ai_packages: dict[str, AiReviewPackage] = {}
        # Only paths an imported answer explicitly left UNSURE may enter the
        # user's final-decision action.
        self._ai_unsure_reasons: dict[str, str] = {}
        self._rule_error = ""
        try:
            self._rules = load_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            # Never overwrite a user-edited invalid file.  The editor can repair
            # it; until then the packaged current configuration remains active.
            self._rules = default_rules()
            self._rule_error = str(error)
        # Classification data is pinned to the scan that produced the rows.
        # Editing thresholds later may change the next scan, never the meaning
        # or execution recheck of an already displayed directory candidate.
        self._scan_rules = self._rules
        self._rule_editor: RuleEditor | None = None
        self._drive_vars: dict[Path, tk.BooleanVar] = {}
        self._buttons: dict[str, ttk.Button] = {}
        # None, "scanning", or "deleting".  Every control consults this.
        self._busy: str | None = None

        self._status = tk.StringVar(value="勾选盘符后点「开始扫描」。")
        self._deletable_total = tk.StringVar(value="—")
        self._unsure_total = tk.StringVar(value="—")

        root.title("DevClean")
        root.geometry("1120x680")
        root.minsize(900, 560)
        self._build()
        self._sync_buttons()
        if self._rule_error:
            self._status.set(f"规则文件需要修正：{self._rule_error}")
        root.after(120, self._drain_events)

    # ---- layout -------------------------------------------------------------

    def _configure_style(self) -> None:
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        self._root.configure(background=_CANVAS)

        style.configure("App.TFrame", background=_CANVAS)
        style.configure("Card.TFrame", background=_SURFACE)
        style.configure("Band.TFrame", background=_INK)
        style.configure(
            "Brand.TLabel",
            background=_INK,
            foreground="#ffffff",
            font=("Segoe UI Semibold", 17),
        )
        style.configure(
            "Tagline.TLabel", background=_INK, foreground="#93a4c4", font=("Segoe UI", 9)
        )
        style.configure(
            "Drive.TCheckbutton",
            background=_INK,
            foreground="#d7e0f2",
            font=("Segoe UI", 10),
        )
        style.map(
            "Drive.TCheckbutton",
            background=[("active", _INK)],
            foreground=[("active", "#ffffff")],
        )
        style.configure(
            "Status.TLabel", background=_CANVAS, foreground="#5b6780", font=("Segoe UI", 9)
        )
        style.configure(
            "CardTitle.TLabel",
            background=_SURFACE,
            foreground=_INK,
            font=("Segoe UI Semibold", 11),
        )
        style.configure(
            "Amount.TLabel",
            background=_SURFACE,
            foreground=_INK,
            font=("Segoe UI Light", 26),
        )
        style.configure(
            "Hint.TLabel", background=_SURFACE, foreground="#78849c", font=("Segoe UI", 9)
        )
        for name, tint in (("Go", _GREEN), ("Warn", _RED), ("Muted", "#5d6b85")):
            style.configure(
                f"{name}.TButton",
                background=tint,
                foreground="#ffffff",
                font=("Segoe UI Semibold", 10),
                borderwidth=0,
                padding=(15, 9),
            )
            style.map(
                f"{name}.TButton", background=[("active", tint), ("disabled", "#c3cad8")]
            )
        style.configure(
            "Rows.Treeview",
            background=_SURFACE,
            fieldbackground=_SURFACE,
            foreground="#243049",
            borderwidth=0,
            rowheight=27,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Rows.Treeview.Heading",
            background="#eef1f7",
            foreground="#5b6780",
            font=("Segoe UI", 9),
            borderwidth=0,
            padding=(8, 6),
        )
        style.map("Rows.Treeview.Heading", background=[("active", "#e5e9f2")])
        style.layout("Rows.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure(
            "Thin.Horizontal.TProgressbar",
            background=_GREEN,
            troughcolor="#33415c",
            borderwidth=0,
            thickness=5,
        )

    def _build(self) -> None:
        self._configure_style()

        band = ttk.Frame(self._root, style="Band.TFrame", padding=(20, 14))
        band.pack(fill=tk.X)
        titles = ttk.Frame(band, style="Band.TFrame")
        titles.pack(side=tk.LEFT)
        ttk.Label(titles, text="DevClean", style="Brand.TLabel").pack(anchor=tk.W)
        ttk.Label(
            titles,
            text="开发工具与 AI 缓存清理 · 扫描与处置规则可编辑",
            style="Tagline.TLabel",
        ).pack(anchor=tk.W, pady=(1, 0))

        picker = ttk.Frame(band, style="Band.TFrame")
        picker.pack(side=tk.RIGHT)
        self._rescan = ttk.Button(
            picker, text="开始扫描", style="Go.TButton", command=self._start_scan
        )
        self._rescan.pack(side=tk.RIGHT, padx=(14, 0))
        self._rule_button = ttk.Button(
            picker,
            text="规则设置",
            style="Muted.TButton",
            command=self._edit_rules,
        )
        self._rule_button.pack(side=tk.RIGHT, padx=(8, 0))
        preferred = _system_drive()
        for drive in reversed(fixed_volume_roots()):
            state = tk.BooleanVar(value=drive == preferred)
            self._drive_vars[drive] = state
            ttk.Checkbutton(
                picker, text=str(drive)[:2], variable=state, style="Drive.TCheckbutton"
            ).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Label(picker, text="盘符", style="Tagline.TLabel").pack(
            side=tk.RIGHT, padx=(0, 4)
        )

        self._progress = ttk.Progressbar(
            self._root, style="Thin.Horizontal.TProgressbar", mode="indeterminate"
        )
        self._progress.pack(fill=tk.X)

        outer = ttk.Frame(self._root, style="App.TFrame", padding=(16, 12, 16, 14))
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, textvariable=self._status, style="Status.TLabel").pack(
            anchor=tk.W, pady=(0, 10)
        )

        buckets = ttk.Frame(outer, style="App.TFrame")
        buckets.pack(fill=tk.BOTH, expand=True)
        buckets.columnconfigure(0, weight=1, uniform="b")
        buckets.columnconfigure(1, weight=1, uniform="b")
        buckets.rowconfigure(0, weight=1)

        self._deletable_tree = self._build_bucket(
            buckets,
            column=0,
            accent=_GREEN,
            title="可以删除",
            hint="工具已确定是缓存或可再生产物。默认全部勾选，点方框可单独取消。",
            total=self._deletable_total,
            buttons=(
                ("all", "全选", "Muted", lambda: self._check_all(True)),
                ("none", "全不选", "Muted", lambda: self._check_all(False)),
                (
                    "recycle",
                    "清理（进回收站）",
                    "Go",
                    lambda: self._delete(irreversible=False),
                ),
                ("purge", "彻底删除", "Warn", lambda: self._delete(irreversible=True)),
            ),
            checkable=True,
        )
        self._unsure_tree = self._build_bucket(
            buckets,
            column=1,
            accent=_AMBER,
            title="不确定，交 AI 判断",
            hint=(
                "工具认不出这些是什么。导回结果后可删的会移到左边；"
                "AI 仍不确定的，选中后由你最后决定。"
            ),
            total=self._unsure_total,
            buttons=(
                ("export", "导出给 AI", "Muted", self._export_for_ai),
                ("import", "导入结果", "Muted", self._import_from_ai),
                ("decide", "我来决定…", "Muted", self._decide_ai_unsure),
                ("forget", "清空判决记录", "Muted", self._forget_verdicts),
            ),
        )
        self._unsure_tree.bind("<<TreeviewSelect>>", lambda _event: self._sync_buttons())

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
        shell = tk.Frame(parent, background=_SURFACE, highlightthickness=0)
        shell.grid(
            row=0, column=column, sticky="nsew", padx=(0, 7) if column == 0 else (7, 0)
        )
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(1, weight=1)
        # A 3px colour bar down the left edge is what separates the two cards at a
        # glance; ttk offers no border colour worth using.
        tk.Frame(shell, background=accent, width=3).grid(
            row=0, column=0, rowspan=3, sticky="ns"
        )

        head = ttk.Frame(shell, style="Card.TFrame", padding=(14, 12, 14, 8))
        head.grid(row=0, column=1, sticky="ew")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(anchor=tk.W)
        ttk.Label(head, textvariable=total, style="Amount.TLabel").pack(anchor=tk.W)
        ttk.Label(head, text=hint, style="Hint.TLabel", wraplength=430).pack(
            anchor=tk.W, pady=(3, 0)
        )

        holder = ttk.Frame(shell, style="Card.TFrame", padding=(14, 0, 14, 0))
        holder.grid(row=1, column=1, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        tree = ttk.Treeview(
            holder,
            columns=("check", "size", "path"),
            show="headings",
            style="Rows.Treeview",
            height=15,
        )
        tree.heading("check", text="", anchor=tk.CENTER)
        tree.heading("size", text="大小", anchor=tk.E)
        tree.heading("path", text="位置", anchor=tk.W)
        tree.column("check", width=32, anchor=tk.CENTER, stretch=False)
        tree.column("size", width=86, anchor=tk.E, stretch=False)
        tree.column("path", width=400, anchor=tk.W)
        tree.tag_configure("odd", background="#fafbfe")
        tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.grid(row=0, column=1, sticky="ns")
        if checkable:
            # ttk has no checkbox cell, so the first column draws one and a click
            # inside that column toggles it.  Clicking anywhere else just selects
            # the row, which never deletes anything on its own.
            tree.bind("<Button-1>", self._on_row_click)

        actions = ttk.Frame(shell, style="Card.TFrame", padding=(14, 12, 14, 14))
        actions.grid(row=2, column=1, sticky="ew")
        for key, label, kind, command in buttons:
            button = ttk.Button(
                actions, text=label, style=f"{kind}.TButton", command=command
            )
            button.pack(side=tk.LEFT, padx=(0, 8))
            self._buttons[key] = button
        return tree

    def _sync_buttons(self) -> None:
        """Enable exactly the actions that are valid right now.

        Scanning and deleting are mutually exclusive, and an action with nothing
        to act on is disabled rather than left clickable -- a second click during
        a long delete used to start a second pass over rows the first had already
        removed.
        """

        busy = self._busy is not None
        ticked = len(self._selected_items())
        ai_unsure_selected = len(self._selected_ai_unsure_items())
        wanted = {
            "scan": not busy,
            "all": not busy and bool(self._deletable),
            "none": not busy and bool(self._checked),
            "recycle": not busy and ticked > 0,
            "purge": not busy and ticked > 0,
            "export": not busy and bool(self._unsure),
            # Import also supports an answer from before a restart through its
            # persisted candidate-id/path session index, so it must not depend on an
            # in-memory package from this process.
            "import": not busy,
            "decide": not busy and ai_unsure_selected > 0,
            "forget": not busy and self._rules.ai_rule_count > 0,
        }
        self._rescan.configure(state=tk.NORMAL if wanted["scan"] else tk.DISABLED)
        self._rule_button.configure(state=tk.NORMAL if not busy else tk.DISABLED)
        for key, button in self._buttons.items():
            button.configure(
                state=tk.NORMAL if wanted.get(key, not busy) else tk.DISABLED
            )
        for state in self._drive_vars.values():
            del state  # checkbuttons stay usable; the scan button is the gate

    # ---- scan ---------------------------------------------------------------

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
        # Completed exports remain recoverable from the bounded session index.
        # Do not retain every old package and all of its TriageItems in memory
        # across scans.
        self._ai_packages.clear()
        self._deletable_tree.delete(*self._deletable_tree.get_children())
        self._unsure_tree.delete(*self._unsure_tree.get_children())
        self._deletable_total.set("—")
        self._unsure_total.set("—")
        self._busy = "scanning"
        self._sync_buttons()
        self._progress.start(60)
        self._status.set("正在扫描…")
        self._scan_rules = self._rules
        self._known_roots = discover_known_cleanup_roots(self._scan_rules.scan)
        roots = scan_targets(self._known_roots, drives, self._scan_rules)
        if not roots:
            self._progress.stop()
            self._busy = None
            self._sync_buttons()
            self._status.set("所选盘符上没有已知的可清理位置。")
            return
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

    def _scan_worker(
        self,
        token: str,
        roots: tuple[Path, ...],
        cancel: CancellationToken,
        active_rules: UserRules,
        known_roots: tuple[KnownCleanupRoot, ...],
    ) -> None:
        def progress(stats: ScanStats) -> None:
            self._events.put(("progress", (token, stats.files, stats.logical_bytes)))

        session = TriageSession()
        now = datetime.now(UTC)
        active_known_roots = (
            known_roots
            if active_rules.scan.include_known_cleanup_roots
            else ()
        )
        configured_skip_paths = {
            normalise_path(path)
            for path in expanded_scan_paths(active_rules.scan.excluded_paths)
        }
        # Protect the actual runtime state location precisely. Packaged builds
        # use DevClean-data beside the EXE; source runs use LocalAppData.
        configured_skip_paths.add(normalise_path(data_dir()))
        if not active_rules.scan.include_known_cleanup_roots:
            # The profile root contains most user-level package caches.  Merely
            # omitting those caches as separate roots would still reach them
            # through the profile walk, so the switch must prune them there too.
            configured_skip_paths.update(
                normalise_path(root.path) for root in known_roots
            )
        # The worker owns the session, so partial updates travel as plain rows.
        # Handing the live session to the UI thread mid-scan would be a race.
        next_publish = time.monotonic() + 1.5
        try:
            for record in scan_roots(
                roots,
                ScanOptions(
                    include_directories=True,
                    exact_file_identity=False,
                    skip_directory_names=frozenset(
                        name.casefold()
                        for name in active_rules.scan.skip_directory_names
                    ),
                    skip_paths=frozenset(configured_skip_paths),
                ),
                cancel,
                progress,
            ):
                if record.kind is ScanRecordKind.FILE:
                    session.add(
                        triage_file(
                            record,
                            known_roots=active_known_roots,
                            delete_config=active_rules.delete.classification,
                            keep_config=active_rules.keep.classification,
                            now=now,
                        )
                    )
                elif record.kind is ScanRecordKind.DIRECTORY:
                    item = triage_directory(
                        record,
                        known_roots=active_known_roots,
                        delete_config=active_rules.delete.classification,
                        keep_config=active_rules.keep.classification,
                    )
                    if item is not None:
                        session.add(item)
                if time.monotonic() >= next_publish:
                    next_publish = time.monotonic() + 1.5
                    self._events.put(
                        ("scan_partial", (token, _rows_of(session, active_rules)))
                    )
        except (OSError, RuntimeError, ValueError) as error:
            self._events.put(("scan_error", (token, str(error))))
            return
        self._events.put(("scan_done", (token, session, cancel.is_cancelled())))

    def _publish(self, session: TriageSession) -> None:
        self._session = session
        previous_deletable = {item.path for item in self._deletable}
        previous_checked = set(self._checked)
        deletable, unsure = _partition_items(session, self._rules)
        self._deletable = sorted(deletable, key=self._size_of, reverse=True)
        self._unsure = sorted(unsure, key=self._size_of, reverse=True)
        current_deletable = {item.path for item in self._deletable}
        # Reclassification must not silently re-check rows the user unticked.
        # Newly promoted rows are selected, matching the existing AI-import UX.
        self._checked = (
            previous_checked & current_deletable
        ) | (current_deletable - previous_deletable)
        self._fill(self._deletable_tree, self._deletable)
        self._fill(self._unsure_tree, self._unsure)
        self._refresh_totals()
        self._sync_buttons()

    _TICKED = "\u2611"
    _UNTICKED = "\u2610"

    def _fill(self, tree: ttk.Treeview, items: Sequence[TriageItem]) -> None:
        self._fill_rows(
            tree,
            tuple(
                (
                    item.path,
                    self._size_of(item),
                    item.target_kind is CleanupTargetKind.DIRECTORY,
                )
                for item in items
            ),
        )

    def _fill_rows(self, tree: ttk.Treeview, rows: Sequence[Row]) -> None:
        tree.delete(*tree.get_children())
        for index, (item_path, size, is_directory) in enumerate(rows[:_ROWS_DRAWN]):
            label = f"[整个目录] {item_path}" if is_directory else item_path
            if tree is not self._deletable_tree:
                mark = ""  # the AI pane is exported whole; nothing to tick
            elif item_path in self._checked:
                mark = self._TICKED
            else:
                mark = self._UNTICKED
            tree.insert(
                "",
                tk.END,
                iid=item_path,
                values=(mark, _format_bytes(size), label),
                tags=("odd",) if index % 2 else (),
            )

    def _on_row_click(self, event: tk.Event) -> str | None:
        tree = self._deletable_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return None
        if tree.identify_column(event.x) != "#1":
            return None
        row = tree.identify_row(event.y)
        if not row:
            return None
        if row in self._checked:
            self._checked.discard(row)
            tree.set(row, "check", self._UNTICKED)
        else:
            self._checked.add(row)
            tree.set(row, "check", self._TICKED)
        self._refresh_totals()
        self._sync_buttons()
        return "break"

    def _check_all(self, checked: bool) -> None:
        tree = self._deletable_tree
        mark = self._TICKED if checked else self._UNTICKED
        self._checked = {item.path for item in self._deletable} if checked else set()
        for row in tree.get_children():
            tree.set(row, "check", mark)
        self._refresh_totals()
        self._sync_buttons()

    def _size_of(self, item: TriageItem) -> int:
        if item.target_kind is not CleanupTargetKind.DIRECTORY:
            return item.logical_size
        session = self._session
        if session is None:
            return 0
        return session.subtree_totals(item.path).logical_bytes

    def _refresh_totals(self) -> None:
        found = sum(self._size_of(item) for item in self._deletable)
        chosen = self._selected_items()
        shown = (
            f"，表中显示前 {_ROWS_DRAWN:,} 项"
            if len(self._deletable) > _ROWS_DRAWN
            else ""
        )
        if len(chosen) == len(self._deletable):
            self._deletable_total.set(
                f"{_format_bytes(found)}（{len(chosen):,} 项{shown}）"
            )
        else:
            self._deletable_total.set(
                f"{_format_bytes(sum(self._size_of(item) for item in chosen))}"
                f" / {_format_bytes(found)}"
                f"（已勾选 {len(chosen):,} / {len(self._deletable):,}）"
            )
        unsure_shown = (
            f"，表中显示前 {_ROWS_DRAWN:,} 项"
            if len(self._unsure) > _ROWS_DRAWN
            else ""
        )
        self._unsure_total.set(
            f"{_format_bytes(sum(self._size_of(item) for item in self._unsure))}"
            f"（{len(self._unsure):,} 项{unsure_shown}）"
        )

    # ---- delete -------------------------------------------------------------

    def _selected_items(self) -> tuple[TriageItem, ...]:
        """Return the ticked rows, with rows another ticked row already covers dropped.

        There is deliberately no "nothing ticked means everything" shortcut: that
        invisible rule turned one exploratory click into a 2.7 GB deletion.

        Select-all necessarily ticks both a whole-directory row and the files
        listed beneath it, and the planner refuses a plan containing both.  The
        directory already removes its contents, so the inner rows are dropped
        here rather than handed to the user as an error to resolve by hand.
        """

        ticked = tuple(item for item in self._deletable if item.path in self._checked)
        enclosing = {
            os.path.normcase(os.path.normpath(item.path)) + os.sep
            for item in ticked
            if item.target_kind is CleanupTargetKind.DIRECTORY
        }
        if not enclosing:
            return ticked

        def covered_by_another(item: TriageItem) -> bool:
            normalized = os.path.normcase(os.path.normpath(item.path))
            return any(
                normalized.startswith(prefix)
                for prefix in enclosing
                if normalized + os.sep != prefix
            )

        return tuple(item for item in ticked if not covered_by_another(item))

    def _selected_ai_unsure_items(self) -> tuple[TriageItem, ...]:
        """Return selected right-pane rows that an AI explicitly left UNSURE."""

        if not hasattr(self, "_unsure_tree"):
            return ()
        selected = set(self._unsure_tree.selection())
        return tuple(
            item
            for item in self._unsure
            if item.path in selected
            and normalise_path(item.path) in self._ai_unsure_reasons
        )


    def _delete(self, *, irreversible: bool) -> None:
        items = self._selected_items()
        if not items:
            messagebox.showinfo("DevClean", "没有勾选任何行。点「全选」，或逐行点前面的方框。")
            return
        mode = CleanupMode.PERMANENT if irreversible else CleanupMode.RECYCLE
        self._busy = "deleting"
        self._sync_buttons()
        self._progress.configure(mode="determinate", maximum=len(items), value=0)
        self._status.set(
            f"正在准备{'彻底删除' if irreversible else '删除（进回收站）'} "
            f"{len(items):,} 项…"
        )
        token = uuid4().hex
        # Candidate construction opens a handle per object to pin its exact
        # identity.  Thousands of those on the UI thread freeze the window, so
        # the whole preparation runs in the worker.
        threading.Thread(
            target=self._delete_worker,
            args=(token, items, mode),
            daemon=True,
        ).start()

    def _delete_worker(
        self, token: str, items: tuple[TriageItem, ...], mode: CleanupMode
    ) -> None:
        candidates: list[ScanCleanupCandidate] = []
        reasons: dict[str, int] = {}
        for item in items:
            try:
                candidates.append(self._candidate(item))
            except (CleanupRefusal, OSError, TypeError, ValueError) as error:
                # "N items could not be processed" answers nothing.  Group by
                # what actually went wrong so the number is explainable.
                reasons[_reason_of(error)] = reasons.get(_reason_of(error), 0) + 1
        if not candidates:
            detail = "；".join(f"{name} {count:,} 项" for name, count in reasons.items())
            self._events.put(
                ("delete_error", (token, f"所有勾选项都无法处理：{detail}", ()))
            )
            return
        results: list[CleanupExecutionResult] = []
        try:
            plan = prepare_cleanup_plan(tuple(candidates))
        except Exception as error:
            self._events.put(("delete_error", (token, str(error), ())))
            return
        done = 0
        for batch in plan.batches:
            try:
                results.append(
                    execute_cleanup_batch(
                        batch,
                        mode,
                        known_roots=self._known_roots,
                        delete_config=self._scan_rules.delete.classification,
                        keep_config=self._scan_rules.keep.classification,
                    )
                )
            except Exception as error:
                # One batch failing says nothing about the next.  Record why and
                # carry on; stopping here is what made a single changed cache
                # file end a 7,000-item run.
                reasons[_reason_of(error)] = reasons.get(_reason_of(error), 0) + 1
            done += len(batch.actions)
            self._events.put(("delete_progress", (token, done, len(plan.actions))))
        self._events.put(("delete_done", (token, tuple(results), reasons)))

    def _candidate(self, item: TriageItem) -> ScanCleanupCandidate:
        if item.target_kind is CleanupTargetKind.DIRECTORY:
            session = self._session
            totals = (
                session.subtree_totals(item.path)
                if session is not None
                else DirectorySubtreeTotals()
            )
            return candidate_from_directory_item(
                item,
                totals,
                known_roots=self._known_roots,
                delete_config=self._scan_rules.delete.classification,
                keep_config=self._scan_rules.keep.classification,
            )
        return candidate_from_triage_item(
            item,
            known_roots=self._known_roots,
            delete_config=self._scan_rules.delete.classification,
            keep_config=self._scan_rules.keep.classification,
        )

    # ---- AI -----------------------------------------------------------------

    def _export_for_ai(self) -> None:
        if not self._unsure:
            messagebox.showinfo("DevClean", "没有需要 AI 判断的项。")
            return
        # Everything is exported.  A model cannot read hundreds of KB in one
        # pass, so the export is split into numbered volumes rather than
        # truncated: a silent "largest 300" leaves the user with no idea which
        # 300 they got.  Import binds to whichever volume an answer came from.
        volumes = [
            self._unsure[offset : offset + _AI_VOLUME_ITEMS]
            for offset in range(0, len(self._unsure), _AI_VOLUME_ITEMS)
        ]
        built: list[tuple[AiReviewPackage, str]] = []
        try:
            for chunk in volumes:
                package = build_ai_review_package(
                    tuple(
                        AiReviewCandidateInput(item=item, hard_protected=False)
                        for item in chunk
                    ),
                    scan_session_id=self._scan_session_id,
                    disclose_full_paths=True,
                )
                built.append((package, serialize_ai_review_package(package) + "\n"))
        except (AiReviewContractError, TypeError, ValueError) as error:
            messagebox.showerror("导出失败", str(error))
            return

        chosen = filedialog.asksaveasfilename(
            title="导出给 AI 判断",
            defaultextension=".json",
            initialfile=f"devclean-ai-{len(self._unsure)}.json",
            filetypes=(("JSON", "*.json"),),
        )
        if not chosen:
            return
        base = Path(chosen)
        written: list[Path] = []
        try:
            for index, (_package, rendered) in enumerate(built, start=1):
                target = (
                    base
                    if len(built) == 1
                    else base.with_name(
                        f"{base.stem}-{index}of{len(built)}{base.suffix}"
                    )
                )
                target.write_text(rendered, encoding="utf-8", newline="\n")
                written.append(target)
        except OSError as error:
            messagebox.showerror("导出失败", str(error))
            return

        for package, _rendered in built:
            self._ai_packages[package.review_session_id] = package
            # Losing the index entry only costs the restart-proof import path.
            with contextlib.suppress(OSError):
                ai_sessions.remember_export(
                    package.review_session_id,
                    {entry.candidate_id: entry.item.path for entry in package.entries},
                )
        self._sync_buttons()
        if len(written) == 1:
            self._status.set(
                f"已导出全部 {len(self._unsure):,} 项到 {written[0]}。"
                "让 AI 按文件里的说明回答，然后点「导入结果」。"
            )
        else:
            self._status.set(
                f"全部 {len(self._unsure):,} 项已分成 {len(written)} 卷导出到 "
                f"{base.parent}，文件名 {base.stem}-1of{len(written)} 起。"
                "每卷分别让 AI 回答，回答完的逐份导入即可。"
            )

    def _import_from_ai(self) -> None:
        source = filedialog.askopenfilename(
            title="导入 AI 结果", filetypes=(("JSON", "*.json"),)
        )
        if not source:
            return
        try:
            text = Path(source).read_text(encoding="utf-8")
        except OSError as error:
            messagebox.showerror("导入失败", str(error))
            return
        # The answer belongs to exactly one volume, and the user picks files by
        # hand, so every exported volume is tried rather than demanding they
        # remember which one this was.
        imported = None
        imported_session = ""
        first_error = ""
        for package in self._ai_packages.values():
            try:
                imported = parse_ai_review_response(text, package)
                imported_session = package.review_session_id
                break
            except (AiReviewContractError, TypeError, ValueError) as error:
                first_error = first_error or str(error)
        rule_verdicts: list[tuple[str, RuleDecision, str]] = []
        recycle: set[str] = set()
        keep: set[str] = set()
        needs_user: set[str] = set()

        def record(path: str, verdict: str, reason: str) -> None:
            key = normalise_path(path)
            if verdict == AiRecommendation.RECOMMEND_RECYCLE.value:
                self._ai_unsure_reasons.pop(key, None)
                recycle.add(key)
                rule_verdicts.append((path, RuleDecision.DELETE, reason))
            elif verdict == AiRecommendation.KEEP.value:
                self._ai_unsure_reasons.pop(key, None)
                keep.add(key)
                rule_verdicts.append((path, RuleDecision.KEEP, reason))
            else:
                needs_user.add(key)
                self._ai_unsure_reasons[key] = reason

        if imported is not None:
            for entry in imported.recommendations:
                record(
                    entry.item.path,
                    {
                        AiRecommendation.RECOMMEND_RECYCLE: (
                            AiRecommendation.RECOMMEND_RECYCLE.value
                        ),
                        AiRecommendation.KEEP: AiRecommendation.KEEP.value,
                    }.get(entry.recommendation, AiRecommendation.UNSURE.value),
                    entry.reason,
                )
        else:
            # The export this answers was made before a restart, so the sealed
            # package is gone.  The session index still knows which path each
            # candidate id meant, and that is enough: importing only files rows into lists,
            # and every object is re-verified by identity at deletion.  Refusing
            # here would throw away answers the user paid a model for.
            recovered = _verdicts_from_session_index(text)
            if not recovered:
                messagebox.showerror(
                    "导入失败", first_error or "这份结果不属于任何已导出的卷。"
                )
                return
            try:
                payload = json.loads(text)
                imported_session = str(payload.get("review_session_id", ""))
            except (AttributeError, TypeError, ValueError):
                imported_session = ""
            for path, (verdict, reason) in recovered.items():
                record(path, verdict, reason)
        rules_saved = True
        try:
            # Merge into the latest on-disk edit instead of overwriting changes
            # made in Notepad while DevClean was open.
            self._rules = add_ai_verdicts(load_rules(), rule_verdicts)
        except (OSError, RuleConfigError, UnicodeError) as error:
            rules_saved = False
            messagebox.showwarning(
                "AI 规则未能保存",
                f"本次分类仍会应用，但规则文件没有更新：{error}",
            )

        # A hand-authored KEEP rule wins even when the imported answer says
        # DELETE.  Apply that precedence immediately, not only on the next scan.
        for key in tuple(recycle):
            if self._rules.decision_for(key) is RuleDecision.KEEP:
                recycle.remove(key)
                keep.add(key)
        if rules_saved and self._session is not None:
            # One classification function owns both queues.  Rebuilding from the
            # completed session applies imported decisions, pre-existing regex
            # rules, and KEEP precedence to rows that were already on the left.
            self._publish(self._session)
            deletable_paths = {
                normalise_path(item.path) for item in self._deletable
            }
            moved_count = len(recycle & deletable_paths)
        else:
            # Saving can fail if an externally edited rule file is invalid.  The
            # answer is still useful for this session, but must update both
            # queues so a KEEP can never remain selected on the left.
            kept_left = {
                item.path
                for item in self._deletable
                if normalise_path(item.path) in keep
            }
            if kept_left:
                self._deletable = [
                    item for item in self._deletable if item.path not in kept_left
                ]
                self._checked -= kept_left
            moved = [
                item
                for item in self._unsure
                if normalise_path(item.path) in recycle
            ]
            settled = recycle | keep
            self._unsure = [
                item
                for item in self._unsure
                if normalise_path(item.path) not in settled
            ]
            if moved:
                self._deletable = sorted(
                    self._deletable + moved, key=self._size_of, reverse=True
                )
                self._checked.update(item.path for item in moved)
            moved_count = len(moved)
            self._fill(self._deletable_tree, self._deletable)
            self._fill(self._unsure_tree, self._unsure)
            self._refresh_totals()
            self._sync_buttons()
        status = (
            f"AI 判定 {moved_count:,} 项可删（已在左边）、{len(keep):,} 项不能删。"
        )
        if needs_user:
            status += (
                f"{len(needs_user):,} 项 AI 仍不确定；"
                "在右侧选中后点“我来决定…”。"
            )
        decided_paths = recycle | keep
        status += (
            f"已写入 {len(decided_paths):,} 条规则，同路径以后不再询问。"
            if rules_saved
            else "本次结果已应用，但未写入规则；下次仍可能再次询问。"
        )
        if (
            rules_saved
            and imported is not None
            and imported_session
            and not needs_user
        ):
            # A strict full-package import has consumed the complete index entry.
            # Fallback imports may intentionally be partial, so those entries
            # remain available until the bounded retention policy removes them.
            with contextlib.suppress(OSError):
                ai_sessions.forget_export(imported_session)
            self._ai_packages.pop(imported_session, None)
        self._status.set(status)

    def _decide_ai_unsure(self) -> None:
        """Persist the user's final decision for AI-UNSURE rows."""

        items = self._selected_ai_unsure_items()
        if not items:
            messagebox.showinfo(
                "DevClean",
                "请先在右侧选中 AI 已明确回答 UNSURE 的项目。",
            )
            return
        preview: list[str] = []
        for item in items[:12]:
            reason = self._ai_unsure_reasons[normalise_path(item.path)]
            preview.append(f"{item.path}\nAI 说明：{reason}")
        if len(items) > 12:
            preview.append(f"……另有 {len(items) - 12:,} 项")
        answer = messagebox.askyesnocancel(
            "AI 仍不确定，交给你决定",
            "\n\n".join(preview)
            + "\n\n选择“是”：记为可以删除并移到左侧；"
            "选择“否”：记为确定保留；选择“取消”：不作修改。",
        )
        if answer is None:
            return
        decision = RuleDecision.DELETE if answer else RuleDecision.KEEP
        verdicts = [
            (
                item.path,
                decision,
                (
                    "AI 仍不确定（"
                    + self._ai_unsure_reasons[normalise_path(item.path)]
                    + "）；用户在 DevClean 界面中最终决定"
                    + ("可删除" if answer else "保留")
                ),
            )
            for item in items
        ]
        rules_saved = True
        try:
            self._rules = add_user_verdicts(load_rules(), verdicts)
        except (OSError, RuleConfigError, UnicodeError) as error:
            rules_saved = False
            messagebox.showwarning(
                "用户决定未能保存",
                f"本次决定仍会应用，但规则文件没有更新：{error}",
            )
        selected_paths = {item.path for item in items}
        for item in items:
            self._ai_unsure_reasons.pop(normalise_path(item.path), None)
        if rules_saved and self._session is not None:
            self._publish(self._session)
        else:
            self._unsure = [
                item for item in self._unsure if item.path not in selected_paths
            ]
            if decision is RuleDecision.DELETE:
                self._deletable = sorted(
                    self._deletable + list(items),
                    key=self._size_of,
                    reverse=True,
                )
                self._checked.update(selected_paths)
            self._fill(self._deletable_tree, self._deletable)
            self._fill(self._unsure_tree, self._unsure)
            self._refresh_totals()
            self._sync_buttons()
        choice = "可以删除，已移到左侧并勾选" if answer else "确定保留"
        persistence = (
            "已写入规则，下次扫描不再询问 AI。"
            if rules_saved
            else "仅本次生效；规则修复前无法长期记住。"
        )
        self._status.set(f"你已决定 {len(items):,} 项{choice}。{persistence}")

    def _forget_verdicts(self) -> None:
        count = self._rules.ai_rule_count
        if not count:
            messagebox.showinfo("DevClean", "还没有记住任何 AI 判决。")
            return
        if not messagebox.askyesno(
            "清空 AI 判决记录",
            f"将删除 {count:,} 条由 AI 添加的规则；手工规则不受影响。继续？",
        ):
            return
        try:
            self._rules = clear_ai_rules(self._rules)
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror("清空失败", str(error))
            return
        if self._session is not None:
            self._publish(self._session)
        self._sync_buttons()
        self._status.set("已清空 AI 判决记录。")

    def _edit_rules(self) -> None:
        raw_documents: tuple[str, str, str] | None = None
        try:
            self._rules = load_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            messagebox.showerror(
                "规则载入失败",
                f"{error}\n\n编辑器会打开磁盘原文，请直接修正后保存。",
            )
        try:
            raw_documents = read_rule_documents(errors="replace")
        except (OSError, RuleConfigError, UnicodeError):
            # If the files cannot be read at all, the last valid in-memory
            # configuration is still a useful recovery starting point.
            raw_documents = None
        self._rule_editor = open_rule_editor(
            self._root,
            self._rules,
            self._rules_saved,
            raw_documents,
        )

    def _rules_saved(self, rules: UserRules) -> None:
        self._rules = rules
        if self._session is not None:
            self._publish(self._session)
        self._sync_buttons()
        self._status.set(
            "DELETE/KEEP 路径规则已应用；扫描范围、阈值和分类表下次扫描生效。"
        )

    # ---- events -------------------------------------------------------------

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    token, files, logical = cast(tuple[str, int, int], payload)
                    if token == self._scan_token:
                        self._status.set(
                            f"正在扫描…已看过 {files:,} 个文件"
                            f"（{_format_bytes(logical)}）"
                        )
                elif kind == "scan_partial":
                    token, buckets = cast(
                        tuple[str, tuple[tuple[Row, ...], tuple[Row, ...]]], payload
                    )
                    if token != self._scan_token:
                        continue
                    # Shown as it is found.  Ticking and deleting stay disabled
                    # until the scan finishes, because a candidate has to be
                    # built from the completed session.
                    partial_deletable, partial_unsure = buckets
                    self._fill_rows(self._deletable_tree, partial_deletable)
                    self._fill_rows(self._unsure_tree, partial_unsure)
                    self._deletable_total.set(
                        f"{_format_bytes(sum(row[1] for row in partial_deletable))}"
                        f"（{len(partial_deletable):,} 项，扫描中）"
                    )
                    self._unsure_total.set(
                        f"{_format_bytes(sum(row[1] for row in partial_unsure))}"
                        f"（{len(partial_unsure):,} 项，扫描中）"
                    )
                elif kind == "scan_done":
                    token, session, cancelled = cast(
                        tuple[str, TriageSession, bool], payload
                    )
                    if token != self._scan_token:
                        continue
                    self._progress.stop()
                    self._busy = None
                    self._publish(session)
                    self._status.set(
                        "扫描已取消，下面是已看到的部分。"
                        if cancelled
                        else "扫描完成。左边是可以删除的，右边需要 AI 判断。"
                    )
                elif kind == "scan_error":
                    token, detail = cast(tuple[str, str], payload)
                    if token != self._scan_token:
                        continue
                    self._progress.stop()
                    self._busy = None
                    self._sync_buttons()
                    self._status.set(f"扫描失败：{detail}")
                elif kind == "delete_progress":
                    _token, done, total = cast(tuple[str, int, int], payload)
                    self._progress.configure(maximum=total, value=done)
                    self._status.set(f"正在删除… {done:,} / {total:,}")
                elif kind == "delete_done":
                    _token, results, reasons = cast(
                        tuple[str, tuple[CleanupExecutionResult, ...], dict[str, int]],
                        payload,
                    )
                    self._progress.stop()
                    self._progress.configure(mode="indeterminate", value=0)
                    self._busy = None
                    self._report_deletion(results, reasons)
                elif kind == "delete_error":
                    _token, detail, results = cast(
                        tuple[str, str, tuple[CleanupExecutionResult, ...]], payload
                    )
                    self._progress.stop()
                    self._progress.configure(mode="indeterminate", value=0)
                    done = sum(
                        1
                        for result in results
                        for _action, state in result.action_states
                        if state in {ActionState.PURGED, ActionState.RECYCLED}
                    )
                    self._busy = None
                    self._sync_buttons()
                    self._status.set(f"删除中断：{detail}（已完成 {done:,} 项）")
                    messagebox.showerror("删除中断", detail)
        except Empty:
            pass
        self._root.after(120, self._drain_events)

    def _report_deletion(
        self,
        results: tuple[CleanupExecutionResult, ...],
        reasons: dict[str, int],
    ) -> None:
        finished = 0
        failed = 0
        freed = 0
        for result in results:
            for _action, state in result.action_states:
                if state in {ActionState.PURGED, ActionState.RECYCLED}:
                    finished += 1
                else:
                    failed += 1
            freed += result.purged_logical_bytes
        gone = {
            item.path
            for item in self._deletable
            if item.path in self._checked and not Path(item.path).exists()
        }
        if gone:
            self._deletable = [
                item for item in self._deletable if item.path not in gone
            ]
            self._checked -= gone
            self._fill(self._deletable_tree, self._deletable)
            self._refresh_totals()
        self._sync_buttons()
        parts = [f"已删除 {finished:,} 项"]
        if freed:
            parts.append(f"释放 {_format_bytes(freed)}")
        if failed:
            parts.append(f"{failed:,} 项未完成")
        for name, count in sorted(reasons.items(), key=lambda pair: -pair[1]):
            parts.append(f"{name} {count:,} 项")
        self._status.set("；".join(parts) + "。正在重新扫描以核对结果…")
        self._status.set(
            self._status.get().replace("正在重新扫描以核对结果…", "点「扫描」可核对结果。")
        )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments == ("--smoke",):
        return 0
    if arguments == ("--ui-smoke",):
        root = tk.Tk()
        root.withdraw()
        DevCleanWindow(root)
        root.update_idletasks()
        root.destroy()
        return 0
    # Administrator is required, not refused: the system garbage that actually
    # fills a drive -- C:\Windows\Temp, SoftwareDistribution\Download, Prefetch,
    # the machine-wide error report queues -- cannot be removed by a normal user.
    root = tk.Tk()
    DevCleanWindow(root)
    root.mainloop()
    return 0


__all__ = [
    "DevCleanWindow",
    "is_ai_review_eligible",
    "is_direct_cleanup_eligible",
    "main",
    "scan_targets",
]
