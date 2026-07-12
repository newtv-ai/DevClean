"""Tkinter GUI for bounded, no-database scan triage."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast

from reclaimer.core.action_history import ActionEvent, ActionHistory
from reclaimer.core.ai_review import (
    AiDecision,
    AiReviewError,
    AiReviewPacket,
    AiReviewResult,
    build_ai_review_packet,
    parse_ai_review_response,
)
from reclaimer.core.auto_clean import (
    permanently_clean_deterministic_record,
    permanently_clean_model_approved_record,
)
from reclaimer.core.cleanup_catalog import (
    CleanupCategory,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
    known_root_for_path,
)
from reclaimer.core.duplicates import DuplicateGroup, DuplicateScanResult, find_large_duplicates
from reclaimer.core.recycle import RecycleRefusal, recycle_targets, target_from_scan_record
from reclaimer.core.triage import ReviewLane, TriageItem, TriageSession, triage_file
from reclaimer.platform.windows.permanent_delete import PermanentDeleteRefusal
from reclaimer.platform.windows.recycle_bin import RecycleBinError, recycle_file
from reclaimer.scanner import CancellationToken, ScanOptions, ScanRecordKind, scan_roots

_LANE_TITLES = {
    ReviewLane.AUTO_CLEAN: "可自动清理",
    ReviewLane.AI_REVIEW: "需要 AI 解释",
    ReviewLane.USER_REVIEW: "需要你决定",
    ReviewLane.PROTECTED: "受保护，不清理",
}

_CATEGORY_TITLES = {
    CleanupCategory.USER_TEMP: "用户临时文件",
    CleanupCategory.CRASH_DUMPS: "崩溃转储",
    CleanupCategory.PIP_CACHE: "pip 缓存",
    CleanupCategory.UV_CACHE: "uv 缓存",
    CleanupCategory.NPM_CACHE: "npm 缓存",
    CleanupCategory.PNPM_STORE: "pnpm store",
    CleanupCategory.HUGGINGFACE_CACHE: "Hugging Face",
    CleanupCategory.GRADLE_CACHE: "Gradle 缓存",
    CleanupCategory.YARN_CACHE: "Yarn 缓存",
    CleanupCategory.OLLAMA_MODELS: "Ollama 模型",
    CleanupCategory.VSCODE_CACHE: "VS Code 缓存",
    CleanupCategory.BROWSER_CACHE: "浏览器缓存",
    CleanupCategory.THUMBNAIL_CACHE: "缩略图缓存",
    CleanupCategory.OTHER: "其它",
}

_MAX_USER_REVIEW_ITEMS = 500


class ReclaimerWindow:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._root.title("Reclaimer — 安全磁盘清理")
        self._root.minsize(1040, 680)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._history = ActionHistory()
        self._session: TriageSession | None = None
        self._ai_packet: AiReviewPacket | None = None
        self._resolved_ai_paths: set[str] = set()
        self._next_display_id = 0
        self._scan_cancel: CancellationToken | None = None
        self._duplicate_cancel: CancellationToken | None = None
        self._last_scan_roots: tuple[Path, ...] = ()
        self._known_roots: tuple[KnownCleanupRoot, ...] = ()
        self._root_path = tk.StringVar(value=str(Path.home()))
        self._status = tk.StringVar(value="选择目录后开始扫描；扫描结果不会写入 SQLite。")
        self._counts = {lane: tk.StringVar(value="0 个文件 · 0 B") for lane in ReviewLane}
        self._trees: dict[ReviewLane, ttk.Treeview] = {}
        self._displayed_items: dict[str, TriageItem] = {}
        self._category_tree: ttk.Treeview | None = None
        self._directory_tree: ttk.Treeview | None = None
        self._duplicates_tree: ttk.Treeview | None = None
        self._history_tree: ttk.Treeview | None = None
        self._insight_note = tk.StringVar()
        self._volume_note = tk.StringVar(value="完成扫描后显示所扫描驱动器的可用空间。")
        self._scan_button: ttk.Button | None = None
        self._catalog_button: ttk.Button | None = None
        self._duplicate_button: ttk.Button | None = None
        self._cancel_button: ttk.Button | None = None
        self._build()
        self._render_history()
        self._root.after(80, self._drain_events)

    def _build(self) -> None:
        content = ttk.Frame(self._root, padding=16)
        content.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(content)
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="扫描目录：").pack(side=tk.LEFT)
        ttk.Entry(controls, textvariable=self._root_path, width=80).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8)
        )
        ttk.Button(controls, text="选择目录", command=self._choose_root).pack(side=tk.LEFT)
        self._scan_button = ttk.Button(controls, text="开始扫描", command=self._start_scan)
        self._scan_button.pack(side=tk.LEFT, padx=(8, 0))
        self._catalog_button = ttk.Button(
            controls, text="扫描常见缓存", command=self._start_known_scan
        )
        self._catalog_button.pack(side=tk.LEFT, padx=(8, 0))
        self._cancel_button = ttk.Button(
            controls, text="停止扫描", command=self._cancel_scan, state=tk.DISABLED
        )
        self._cancel_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            content,
            text=(
                "“扫描常见缓存”会扫描当前用户 TEMP、崩溃转储和常见开发/AI 缓存。仅超过 7 天、"
                "通过句柄复核的 TEMP/崩溃转储会自动永久清理；开发缓存先交给 AI 逐项解释。"
            ),
            wraplength=960,
        ).pack(fill=tk.X, pady=(10, 6))

        notebook = ttk.Notebook(content)
        notebook.pack(fill=tk.BOTH, expand=True)
        insight_frame = ttk.Frame(notebook, padding=8)
        notebook.add(insight_frame, text="空间概览")
        ttk.Label(insight_frame, textvariable=self._volume_note).pack(anchor=tk.W, pady=(0, 8))
        ttk.Label(insight_frame, text="按清理类别").pack(anchor=tk.W)
        self._category_tree = ttk.Treeview(
            insight_frame,
            columns=("category", "files", "space"),
            show="headings",
            height=7,
        )
        self._category_tree.heading("category", text="类别")
        self._category_tree.heading("files", text="文件数")
        self._category_tree.heading("space", text="已分配空间")
        self._category_tree.column("category", width=240)
        self._category_tree.column("files", width=100, anchor=tk.E)
        self._category_tree.column("space", width=140, anchor=tk.E)
        self._category_tree.pack(fill=tk.X, pady=(4, 12))
        ttk.Label(insight_frame, text="扫描根目录下占用最多的位置").pack(anchor=tk.W)
        self._directory_tree = ttk.Treeview(
            insight_frame,
            columns=("path", "files", "space"),
            show="headings",
        )
        self._directory_tree.heading("path", text="目录")
        self._directory_tree.heading("files", text="文件数")
        self._directory_tree.heading("space", text="已分配空间")
        self._directory_tree.column("path", width=700)
        self._directory_tree.column("files", width=100, anchor=tk.E)
        self._directory_tree.column("space", width=140, anchor=tk.E)
        self._directory_tree.pack(fill=tk.BOTH, expand=True, pady=(4, 4))
        ttk.Label(insight_frame, textvariable=self._insight_note).pack(anchor=tk.W)
        duplicate_frame = ttk.Frame(notebook, padding=8)
        notebook.add(duplicate_frame, text="重复文件")
        ttk.Label(
            duplicate_frame,
            text="仅检测大于等于 1 MiB 的普通文件；按内容 SHA-256 精确匹配。"
            "每组会自动保留一个基准文件，"
            "其余副本进入“需要你决定”。",
            wraplength=960,
        ).pack(fill=tk.X, pady=(0, 6))
        self._duplicates_tree = ttk.Treeview(
            duplicate_frame,
            columns=("copies", "reclaimable", "keep", "digest"),
            show="headings",
        )
        self._duplicates_tree.heading("copies", text="相同副本")
        self._duplicates_tree.heading("reclaimable", text="可释放（逻辑）")
        self._duplicates_tree.heading("keep", text="自动保留的基准文件")
        self._duplicates_tree.heading("digest", text="SHA-256")
        self._duplicates_tree.column("copies", width=100, anchor=tk.E)
        self._duplicates_tree.column("reclaimable", width=140, anchor=tk.E)
        self._duplicates_tree.column("keep", width=580)
        self._duplicates_tree.column("digest", width=180)
        self._duplicates_tree.pack(fill=tk.BOTH, expand=True)
        history_frame = ttk.Frame(notebook, padding=8)
        notebook.add(history_frame, text="操作历史")
        ttk.Label(
            history_frame,
            text="最多保留当前 1 MiB 的脱敏 JSONL 历史；永久删除不能从这里恢复。",
        ).pack(anchor=tk.W, pady=(0, 6))
        self._history_tree = ttk.Treeview(
            history_frame,
            columns=("time", "action", "category", "size", "path", "detail"),
            show="headings",
        )
        for column, title, width in (
            ("time", "时间", 180),
            ("action", "操作", 150),
            ("category", "类别", 130),
            ("size", "大小", 100),
            ("path", "路径", 260),
            ("detail", "说明", 240),
        ):
            self._history_tree.heading(column, text=title)
            self._history_tree.column(column, width=width)
        self._history_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Button(history_frame, text="刷新历史", command=self._render_history).pack(
            anchor=tk.E, pady=(6, 0)
        )
        for lane in ReviewLane:
            frame = ttk.Frame(notebook, padding=8)
            notebook.add(frame, text=_LANE_TITLES[lane])
            ttk.Label(frame, textvariable=self._counts[lane]).pack(anchor=tk.W, pady=(0, 6))
            tree = ttk.Treeview(
                frame,
                columns=("category", "size", "reason", "path"),
                show="headings",
                selectmode="extended",
            )
            tree.heading("category", text="类别")
            tree.heading("size", text="大小")
            tree.heading("reason", text="判定依据")
            tree.heading("path", text="路径")
            tree.column("category", width=130, stretch=False)
            tree.column("size", width=100, stretch=False, anchor=tk.E)
            tree.column("reason", width=250, stretch=False)
            tree.column("path", width=650, stretch=True)
            tree.pack(fill=tk.BOTH, expand=True)
            self._trees[lane] = tree

        actions = ttk.Frame(content)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(
            actions,
            text="复制 AI 审查包",
            command=self._copy_ai_review_packet,
        ).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="粘贴 AI 回复并处理确定项",
            command=self._open_ai_response_dialog,
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            actions,
            text="将选中的“需要你决定”项移到回收站",
            command=self._recycle_selected_user_items,
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._duplicate_button = ttk.Button(
            actions,
            text="检查大文件重复项",
            command=self._start_duplicate_scan,
            state=tk.DISABLED,
        )
        self._duplicate_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            actions,
            text="扫描不会创建 SQLite 索引；只保留每栏最大的 500 项用于显示和 AI 审查。",
        ).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Separator(content).pack(fill=tk.X, pady=(10, 6))
        ttk.Label(content, textvariable=self._status).pack(anchor=tk.W)

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self._root_path.get() or str(Path.home()))
        if selected:
            self._root_path.set(selected)

    def _start_scan(self) -> None:
        root = Path(self._root_path.get()).expanduser()
        if not root.is_dir():
            messagebox.showerror("Reclaimer", "请选择一个存在的目录。")
            return
        self._begin_scan((root,), scan_label="所选目录")

    def _start_known_scan(self) -> None:
        known_roots = discover_known_cleanup_roots()
        roots = tuple(root.path for root in known_roots)
        if not roots:
            messagebox.showinfo("Reclaimer", "没有发现可扫描的本地常见缓存目录。")
            return
        self._begin_scan(roots, scan_label=f"{len(roots)} 个常见缓存目录")

    def _begin_scan(self, roots: tuple[Path, ...], *, scan_label: str) -> None:
        if self._scan_button is not None:
            self._scan_button.configure(state=tk.DISABLED)
        if self._catalog_button is not None:
            self._catalog_button.configure(state=tk.DISABLED)
        if self._duplicate_button is not None:
            self._duplicate_button.configure(state=tk.DISABLED)
        cancel = CancellationToken()
        self._scan_cancel = cancel
        if self._cancel_button is not None:
            self._cancel_button.configure(state=tk.NORMAL)
        self._ai_packet = None
        self._resolved_ai_paths.clear()
        self._clear_trees()
        known_roots = discover_known_cleanup_roots()
        self._last_scan_roots = roots
        self._known_roots = known_roots
        if self._duplicates_tree is not None:
            self._duplicates_tree.delete(*self._duplicates_tree.get_children())
        self._status.set(
            f"正在扫描{scan_label}；确定安全的旧 TEMP/崩溃转储会被立即永久清理；"
            "不会创建 SQLite 索引…"
        )
        worker = threading.Thread(
            target=self._scan_worker,
            args=(roots, cancel, known_roots),
            daemon=True,
        )
        worker.start()

    def _cancel_scan(self) -> None:
        token = self._scan_cancel or self._duplicate_cancel
        if token is None:
            return
        token.cancel()
        if self._cancel_button is not None:
            self._cancel_button.configure(state=tk.DISABLED)
        self._status.set("正在停止任务；不会再处理新的文件。")

    def _start_duplicate_scan(self) -> None:
        if not self._last_scan_roots:
            messagebox.showinfo("Reclaimer", "请先完成一次目录或常见缓存扫描。")
            return
        if self._scan_cancel is not None or self._duplicate_cancel is not None:
            messagebox.showinfo("Reclaimer", "请等待当前任务结束。")
            return
        cancel = CancellationToken()
        self._duplicate_cancel = cancel
        for button in (self._scan_button, self._catalog_button, self._duplicate_button):
            if button is not None:
                button.configure(state=tk.DISABLED)
        if self._cancel_button is not None:
            self._cancel_button.configure(state=tk.NORMAL)
        self._status.set("正在查找大文件重复项；只读取候选文件以计算 SHA-256，不会删除文件…")
        threading.Thread(
            target=self._duplicate_worker,
            args=(self._last_scan_roots, cancel),
            daemon=True,
        ).start()

    def _duplicate_worker(self, roots: tuple[Path, ...], cancel: CancellationToken) -> None:
        try:
            result = find_large_duplicates(roots, cancel=cancel)
        except (OSError, RuntimeError, ValueError) as error:
            self._events.put(("duplicate_error", str(error)))
            return
        self._events.put(("duplicates", result))

    def _scan_worker(
        self,
        roots: tuple[Path, ...],
        cancel: CancellationToken,
        known_roots: tuple[KnownCleanupRoot, ...],
    ) -> None:
        session = TriageSession()
        observed = 0
        cleaned_files = 0
        cleaned_bytes = 0
        skipped_auto_clean = 0
        temp_root = Path(tempfile.gettempdir())
        try:
            for record in scan_roots(
                roots, ScanOptions(include_directories=False), cancel=cancel
            ):
                if record.kind is not ScanRecordKind.FILE:
                    continue
                item = triage_file(record, temp_root=temp_root, known_roots=known_roots)
                if item.lane is ReviewLane.AUTO_CLEAN:
                    try:
                        known_root = known_root_for_path(Path(record.path), known_roots)
                        approved_root = known_root.path if known_root is not None else temp_root
                        permanently_clean_deterministic_record(
                            record, approved_root=approved_root
                        )
                    except (OSError, PermanentDeleteRefusal, ValueError):
                        skipped_auto_clean += 1
                        item = replace(
                            item,
                            lane=ReviewLane.USER_REVIEW,
                            reason="自动清理跳过：文件已变化、被占用或无法句柄复核；请由你决定",
                        )
                    else:
                        cleaned_files += 1
                        cleaned_bytes += record.allocated_size or record.logical_size
                        self._write_history(
                            item,
                            action="AUTO_PERMANENT",
                            detail="确定性规则与删除句柄复核通过",
                        )
                        item = replace(item, reason=f"已自动永久清理：{item.reason}")
                session.add(item)
                observed += 1
                if observed % 256 == 0:
                    self._events.put(
                        (
                            "progress",
                            (observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session),
                        )
                    )
        except OSError as error:
            self._events.put(("error", str(error)))
            return
        self._events.put(
            (
                "finished",
                (
                    observed,
                    cleaned_files,
                    cleaned_bytes,
                    skipped_auto_clean,
                    session,
                    cancel.is_cancelled(),
                ),
            )
        )

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    observed, cleaned_files, cleaned_bytes, skipped_auto_clean, session = cast(
                        tuple[int, int, int, int, TriageSession], payload
                    )
                    self._update_summary(session)
                    self._status.set(
                        f"已扫描 {observed:,} 个文件；已自动清理 {cleaned_files:,} 个"
                        f"（{_format_bytes(cleaned_bytes)}）；跳过 {skipped_auto_clean:,} 个…"
                    )
                elif kind == "finished":
                    (
                        observed,
                        cleaned_files,
                        cleaned_bytes,
                        skipped_auto_clean,
                        session,
                        cancelled,
                    ) = cast(
                        tuple[int, int, int, int, TriageSession, bool], payload
                    )
                    self._session = session
                    self._render_session(session)
                    self._render_history()
                    summary_word = "扫描已停止" if cancelled else "扫描完成"
                    self._status.set(
                        f"{summary_word}：{observed:,} 个文件；已自动永久清理 {cleaned_files:,} 个"
                        f"（{_format_bytes(cleaned_bytes)}），跳过 {skipped_auto_clean:,} 个；"
                        "仅保留每栏最大的 500 项。"
                    )
                    if self._scan_button is not None:
                        self._scan_button.configure(state=tk.NORMAL)
                    if self._catalog_button is not None:
                        self._catalog_button.configure(state=tk.NORMAL)
                    if self._duplicate_button is not None:
                        self._duplicate_button.configure(state=tk.NORMAL)
                    if self._cancel_button is not None:
                        self._cancel_button.configure(state=tk.DISABLED)
                    self._scan_cancel = None
                elif kind == "duplicates":
                    result = cast(DuplicateScanResult, payload)
                    user_candidates = self._render_duplicate_groups(result)
                    added_candidates = self._append_duplicate_user_candidates(result.groups)
                    qualifier = "；结果已截断" if result.truncated else ""
                    if result.cancelled:
                        qualifier += "；任务已停止"
                    self._status.set(
                        f"重复文件检查完成：{len(result.groups)} 组，"
                        f"已哈希 {result.files_hashed:,} 个文件；"
                        f"{added_candidates}/{user_candidates} 个可回收副本"
                        f"已转入“需要你决定”{qualifier}。"
                    )
                    for button in (self._scan_button, self._catalog_button, self._duplicate_button):
                        if button is not None:
                            button.configure(state=tk.NORMAL)
                    if self._cancel_button is not None:
                        self._cancel_button.configure(state=tk.DISABLED)
                    self._duplicate_cancel = None
                elif kind == "duplicate_error":
                    messagebox.showerror("重复文件检查失败", str(payload))
                    for button in (self._scan_button, self._catalog_button, self._duplicate_button):
                        if button is not None:
                            button.configure(state=tk.NORMAL)
                    if self._cancel_button is not None:
                        self._cancel_button.configure(state=tk.DISABLED)
                    self._duplicate_cancel = None
                elif kind == "error":
                    messagebox.showerror("Reclaimer", f"扫描失败：{payload}")
                    if self._scan_button is not None:
                        self._scan_button.configure(state=tk.NORMAL)
                    if self._catalog_button is not None:
                        self._catalog_button.configure(state=tk.NORMAL)
                    if self._duplicate_button is not None:
                        self._duplicate_button.configure(state=tk.NORMAL)
                    if self._cancel_button is not None:
                        self._cancel_button.configure(state=tk.DISABLED)
                    self._scan_cancel = None
        except queue.Empty:
            pass
        self._root.after(80, self._drain_events)

    def _render_session(self, session: TriageSession) -> None:
        self._clear_trees()
        self._update_summary(session)
        self._render_insights(session)
        self._render_volume_summary()
        for lane in ReviewLane:
            tree = self._trees[lane]
            for index, item in enumerate(session.items(lane)):
                item_id = f"{lane.value}:{index}"
                self._displayed_items[item_id] = item
                tree.insert(
                    "",
                    tk.END,
                    iid=item_id,
                    values=(
                        _category_label(item.category),
                        _format_bytes(item.logical_size),
                        item.reason,
                        item.path,
                    ),
                )

    def _update_summary(self, session: TriageSession) -> None:
        for lane in ReviewLane:
            summary = session.summary(lane)
            self._counts[lane].set(
                f"{summary.files:,} 个文件 · {_format_bytes(summary.logical_bytes)}"
            )

    def _clear_trees(self) -> None:
        self._displayed_items.clear()
        for tree in self._trees.values():
            tree.delete(*tree.get_children())

    def _render_insights(self, session: TriageSession) -> None:
        if self._category_tree is None or self._directory_tree is None:
            return
        self._category_tree.delete(*self._category_tree.get_children())
        self._directory_tree.delete(*self._directory_tree.get_children())
        for category, summary in session.insights.category_items():
            space = summary.allocated_bytes or summary.logical_bytes
            self._category_tree.insert(
                "",
                tk.END,
                values=(_category_label(category), f"{summary.files:,}", _format_bytes(space)),
            )
        for insight in session.insights.top_directories():
            space = insight.summary.allocated_bytes or insight.summary.logical_bytes
            self._directory_tree.insert(
                "",
                tk.END,
                values=(insight.path, f"{insight.summary.files:,}", _format_bytes(space)),
            )
        skipped = session.insights.skipped_directory_buckets
        self._insight_note.set(
            "目录概览已截断：扫描根目录下的首层目录超过 2,000 个。"
            if skipped
            else "目录概览按扫描根目录的首层聚合；类别统计始终完整。"
        )

    def _render_volume_summary(self) -> None:
        observed: set[str] = set()
        summaries: list[str] = []
        for root in self._last_scan_roots:
            anchor = root.anchor or str(root)
            key = anchor.casefold()
            if key in observed:
                continue
            observed.add(key)
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            summaries.append(
                f"{anchor}：可用 {_format_bytes(usage.free)} / 总计 {_format_bytes(usage.total)}"
            )
        self._volume_note.set("；".join(summaries) if summaries else "无法读取所扫描驱动器的空间。")

    def _render_duplicate_groups(self, result: DuplicateScanResult) -> int:
        if self._duplicates_tree is None:
            return 0
        self._duplicates_tree.delete(*self._duplicates_tree.get_children())
        user_candidates = 0
        for group in result.groups:
            baseline = group.records[0]
            user_candidates += len(group.records) - 1
            self._duplicates_tree.insert(
                "",
                tk.END,
                values=(
                    len(group.records),
                    _format_bytes(group.reclaimable_logical_bytes),
                    baseline.path,
                    group.digest,
                ),
            )
        return user_candidates

    def _append_duplicate_user_candidates(self, groups: tuple[DuplicateGroup, ...]) -> int:
        added = 0
        for group in groups:
            baseline = group.records[0]
            for record in group.records[1:]:
                item = triage_file(
                    record,
                    temp_root=Path(tempfile.gettempdir()),
                    known_roots=self._known_roots,
                )
                if self._add_user_review_item(
                    replace(
                        item,
                        lane=ReviewLane.USER_REVIEW,
                        reason=(
                            "与基准文件 SHA-256 完全相同；为避免全部删除已自动保留基准："
                            f"{_short_text(baseline.path, limit=120)}"
                        ),
                    )
                ):
                    added += 1
        return added

    def _recycle_selected_user_items(self) -> None:
        tree = self._trees[ReviewLane.USER_REVIEW]
        item_ids = tree.selection()
        if not item_ids:
            messagebox.showinfo("Reclaimer", "请先在“需要你决定”栏选择要移到回收站的文件。")
            return
        if len(item_ids) > 32:
            messagebox.showerror("Reclaimer", "一次最多选择 32 个文件。")
            return
        items = [self._displayed_items[item_id] for item_id in item_ids]
        paths = "\n".join(item.path for item in items)
        approved = messagebox.askyesno(
            "确认移到回收站",
            "以下经过扫描快照复核的文件将移到 Windows 回收站：\n\n"
            f"{paths}\n\n"
            "若文件在扫描后发生变化、被占用或不再安全，操作会被拒绝。",
        )
        if not approved:
            return
        try:
            targets = tuple(
                target_from_scan_record(item.record, candidate_id=f"gui_recycle_{index}")
                for index, item in enumerate(items, start=1)
            )
            recycled = recycle_targets(targets, recycle_file)
        except (OSError, RecycleBinError, RecycleRefusal, ValueError) as error:
            messagebox.showerror("未移动", f"没有完成回收站操作：{error}")
            return
        for item_id in item_ids:
            tree.delete(item_id)
            self._displayed_items.pop(item_id, None)
        for item in items:
            self._write_history(item, action="USER_RECYCLE", detail="用户确认后移入 Windows 回收站")
        self._render_history()
        self._status.set(f"已将 {len(recycled)} 个你确认的文件移到 Windows 回收站。")

    def _copy_ai_review_packet(self) -> None:
        if self._session is None:
            messagebox.showinfo("Reclaimer", "请先完成一次扫描。")
            return
        pending = tuple(
            item
            for item in self._session.items(ReviewLane.AI_REVIEW)
            if item.path not in self._resolved_ai_paths
        )
        if not pending:
            messagebox.showinfo("Reclaimer", "当前显示的 AI 审查项都已处理。")
            return
        try:
            packet = build_ai_review_packet(pending[:50])
        except AiReviewError as error:
            messagebox.showerror("AI 审查包", str(error))
            return
        self._ai_packet = packet
        self._root.clipboard_clear()
        self._root.clipboard_append(json.dumps(packet.payload(), ensure_ascii=False, indent=2))
        self._status.set(
            f"已复制 {len(packet.entries)} 项 AI 审查包（不含文件内容）；"
            "将模型回复粘贴回来即可处理。"
        )

    def _open_ai_response_dialog(self) -> None:
        if self._ai_packet is None:
            messagebox.showinfo(
                "Reclaimer", "请先复制一个 AI 审查包，并让模型按其中的 response_contract 回复。"
            )
            return
        dialog = tk.Toplevel(self._root)
        dialog.title("粘贴 AI 审查回复")
        dialog.minsize(760, 480)
        dialog.transient(self._root)
        dialog.grab_set()
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            content,
            text="只接受当前审查包要求的 JSON。DELETE 会对该批次的精确扫描文件"
            "做句柄复核后永久清理；"
            "UNSURE 会转入“需要你决定”。",
            wraplength=720,
        ).pack(fill=tk.X, pady=(0, 8))
        response = ScrolledText(content, wrap=tk.WORD, height=20)
        response.pack(fill=tk.BOTH, expand=True)
        buttons = ttk.Frame(content)
        buttons.pack(fill=tk.X, pady=(8, 0))

        def apply_response() -> None:
            packet = self._ai_packet
            if packet is None:
                return
            try:
                results = parse_ai_review_response(response.get("1.0", tk.END), packet)
            except AiReviewError as error:
                messagebox.showerror("AI 回复无效", str(error), parent=dialog)
                return
            dialog.destroy()
            self._apply_ai_results(results)

        ttk.Button(buttons, text="处理此回复", command=apply_response).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))

    def _apply_ai_results(self, results: tuple[AiReviewResult, ...]) -> None:
        deleted = 0
        kept = 0
        user_review = 0
        for result in results:
            self._resolved_ai_paths.add(result.item.path)
            if result.decision is AiDecision.DELETE:
                try:
                    permanently_clean_model_approved_record(result.item.record)
                except (OSError, PermanentDeleteRefusal, ValueError) as error:
                    if self._move_ai_item_to_user_review(
                        result.item,
                        f"AI 建议删除但句柄复核失败；请由你决定：{_short_text(str(error))}",
                    ):
                        user_review += 1
                else:
                    self._remove_displayed_ai_item(result.item)
                    self._write_history(
                        result.item,
                        action="AI_PERMANENT",
                        detail=f"AI 审查 DELETE：{_short_text(result.explanation)}",
                    )
                    deleted += 1
            elif result.decision is AiDecision.KEEP:
                self._mark_displayed_ai_item(
                    result.item, f"AI 建议保留：{_short_text(result.explanation)}"
                )
                kept += 1
            else:
                if self._move_ai_item_to_user_review(
                    result.item, f"AI 未确认：{_short_text(result.explanation)}"
                ):
                    user_review += 1
        self._ai_packet = None
        self._render_history()
        self._status.set(
            f"AI 审查已处理：永久清理 {deleted} 项，建议保留 {kept} 项，"
            f"转入你决定 {user_review} 项。"
        )

    def _find_displayed_item_id(self, item: TriageItem) -> str | None:
        return next(
            (item_id for item_id, displayed in self._displayed_items.items() if displayed is item),
            None,
        )

    def _remove_displayed_ai_item(self, item: TriageItem) -> None:
        item_id = self._find_displayed_item_id(item)
        if item_id is not None:
            self._trees[ReviewLane.AI_REVIEW].delete(item_id)
            self._displayed_items.pop(item_id, None)

    def _mark_displayed_ai_item(self, item: TriageItem, reason: str) -> None:
        item_id = self._find_displayed_item_id(item)
        if item_id is None:
            return
        updated = replace(item, reason=reason)
        self._displayed_items[item_id] = updated
        self._trees[ReviewLane.AI_REVIEW].item(
            item_id,
            values=(
                _category_label(updated.category),
                _format_bytes(updated.logical_size),
                updated.reason,
                updated.path,
            ),
        )

    def _move_ai_item_to_user_review(self, item: TriageItem, reason: str) -> bool:
        tree = self._trees[ReviewLane.USER_REVIEW]
        if len(tree.get_children()) >= _MAX_USER_REVIEW_ITEMS:
            self._mark_displayed_ai_item(
                item, "AI 未确认，但用户决定栏已满；请缩小扫描目录后继续处理"
            )
            return False
        self._remove_displayed_ai_item(item)
        updated = replace(item, lane=ReviewLane.USER_REVIEW, reason=reason)
        return self._add_user_review_item(updated)

    def _add_user_review_item(self, item: TriageItem) -> bool:
        """Display one exact scanned file in the explicit user-decision lane."""

        tree = self._trees[ReviewLane.USER_REVIEW]
        if len(tree.get_children()) >= _MAX_USER_REVIEW_ITEMS:
            return False
        item_id = f"{ReviewLane.USER_REVIEW}:review-{self._next_display_id}"
        self._next_display_id += 1
        self._displayed_items[item_id] = item
        tree.insert(
            "",
            tk.END,
            iid=item_id,
            values=(
                _category_label(item.category),
                _format_bytes(item.logical_size),
                item.reason,
                item.path,
            ),
        )
        return True

    def _write_history(self, item: TriageItem, *, action: str, detail: str) -> None:
        try:
            self._history.append(
                ActionEvent.create(
                    action=action,
                    category=item.category.value,
                    path=item.path,
                    logical_size=item.logical_size,
                    detail=detail,
                )
            )
        except (OSError, ValueError):
            # History loss must never turn a safe deletion refusal into a retry or broader action.
            return

    def _render_history(self) -> None:
        if self._history_tree is None:
            return
        self._history_tree.delete(*self._history_tree.get_children())
        for event in self._history.recent():
            self._history_tree.insert(
                "",
                tk.END,
                values=(
                    event.occurred_at.replace("T", " ").replace("+00:00", "Z"),
                    event.action,
                    event.category,
                    _format_bytes(event.logical_size),
                    event.path,
                    event.detail,
                ),
            )


def _review_item(item: TriageItem) -> dict[str, object]:
    return {
        "path": _redact_path(item.path),
        "logical_size": item.logical_size,
        "reason": item.reason,
    }


def _redact_path(path: str) -> str:
    home = str(Path.home())
    return path.replace(home, "<USER>", 1)


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


def _short_text(value: str, *, limit: int = 180) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"


def _category_label(category: CleanupCategory) -> str:
    return _CATEGORY_TITLES[category]


def main(argv: Sequence[str] | None = None) -> int:
    if tuple(sys.argv[1:] if argv is None else argv) == ("--smoke",):
        return 0
    root = tk.Tk()
    ReclaimerWindow(root)
    root.mainloop()
    return 0


__all__ = ["main"]
