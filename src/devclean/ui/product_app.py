"""Product shell for DevClean's rule-first, AI-assisted cleanup workflow.

There is one scan button and one decision engine. Known whole-tree rules are
handled as directory objects using a lightweight aggregate metadata pass; their
children are not fed through per-file classification. Only unresolved areas are
walked file-by-file, and residual reviewable ambiguity is routed to the AI lane.

There is deliberately no separate "tool center": a rule that DevClean knows
belongs in this pipeline rather than in a manual per-tool dialog.
"""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import contextlib
import os
import threading
import time
import tkinter as tk
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from uuid import uuid4

from devclean.core.application_cleanup import (
    DecisionOwner,
    clear_process_cache,
    scan_root_snapshot,
)
from devclean.core.cleanup_catalog import (
    CleanupPolicy,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.paths import data_dir
from devclean.core.review_routing import route_unresolved_file_to_ai
from devclean.core.temp_folders import TempFolderScan
from devclean.core.triage import (
    CleanupTargetKind,
    DirectorySubtreeTotals,
    TriageSession,
    triage_directory,
    triage_file,
    triage_temp_folder,
)
from devclean.core.user_rules import (
    RuleConfigError,
    UserRules,
    expanded_scan_paths,
    load_rules,
    normalise_path,
)
from devclean.core.whole_tree_policy import (
    WholeTreePolicyEvidence,
    WholeTreePolicyRefusal,
    assess_application_whole_tree_policy,
)
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.platform.windows.volumes import fixed_volume_roots
from devclean.scanner import (
    CancellationToken,
    ScanOptions,
    ScanRecord,
    ScanRecordKind,
    ScanStats,
    scan_roots,
)
from devclean.scanner.tree_summary import (
    SkipReason,
    TreeSummary,
    TreeSummaryIncomplete,
    summarize_tree,
)
from devclean.ui import app
from devclean.ui import modern_app as modern
from devclean.ui.modern_app import ModernDevCleanWindow

# The live scan clock refreshes about once a second while a scan runs.
_SCAN_CLOCK_INTERVAL_MS = 1000


def _live_elapsed_text(elapsed: float) -> str:
    """Elapsed scan time in whole seconds: "45 秒", "1 分 05 秒"."""

    whole = max(0, int(elapsed))
    if whole < 60:
        return f"扫描耗时：{whole} 秒"
    minutes, seconds = divmod(whole, 60)
    return f"扫描耗时：{minutes} 分 {seconds:02d} 秒"


_SKIP_REASON_TEXT = {
    SkipReason.IN_USE: "正被程序使用",
    SkipReason.DENIED: "没有读取权限",
    SkipReason.CHANGED: "扫描时被换成了链接或其他对象",
    SkipReason.UNREADABLE: "读取出错",
}


def skipped_entries_note(summary: TreeSummary) -> str:
    """What a whole-tree summary could not read, in the user's terms (or "")."""

    if not summary.skipped:
        return ""
    reasons = "、".join(
        f"{_SKIP_REASON_TEXT[reason]} {count:,} 项" for reason, count in summary.skipped_by_reason
    )
    examples = "；".join(
        f"{path}（{_SKIP_REASON_TEXT[reason]}）" for path, reason in summary.skipped_examples[:3]
    )
    return (
        f"扫描时其中 {summary.skipped:,} 项读不到，已跳过、未计入大小（{reasons}）；"
        f"例：{examples}"
    )


def _left_out_note(session: object) -> str:
    """What the scan could not look at, so nobody takes it for everything (or "")."""

    notes: dict[str, str] = getattr(session, "summary_notes", None) or {}
    unlisted: list[str] = getattr(session, "unlisted_roots", None) or []
    text = ""
    if notes:
        skipped = getattr(session, "summary_skipped", 0)
        shown = "、".join(list(notes)[:3]) + ("等" if len(notes) > 3 else "")
        text += (
            f"其中 {len(notes):,} 个应用缓存目录里有 {skipped:,} 项读不到，已跳过、未计入大小，"
            f"这些目录的其余部分照常列出（{shown}；清理后见「清理结果」）。"
        )
    if unlisted:
        shown = "、".join(unlisted[:3]) + ("等" if len(unlisted) > 3 else "")
        text += (
            f"{len(unlisted):,} 个应用缓存目录本身无法读取或无法确认是同一个目录，"
            f"整项未列出，里面的文件也没有逐个列出（{shown}）。"
        )
    return text


class _HybridTriageSession(TriageSession):
    """Triage session that can accept an aggregate for a known directory."""

    # Files skipped because classifying them raised; they are never offered.
    unclassified_files: int = 0

    def __init__(self, *, review_sample_per_category: int) -> None:
        super().__init__(review_sample_per_category=review_sample_per_category)
        # Whole-tree cache rows listed although some entries in them could not
        # be read: the note shown for each, by path.
        self.summary_notes: dict[str, str] = {}
        self.summary_skipped = 0
        # Whole-tree cache roots whose own folder could not be read (or whose
        # identity could not be pinned): not listed at all.
        self.unlisted_roots: list[str] = []

    def record_directory_summary(self, path: str, summary: TreeSummary) -> None:
        key = os.path.normcase(os.path.normpath(path))
        if key not in self._directory_totals:
            raise ValueError("directory must be registered before its summary")
        self._directory_totals[key] = DirectorySubtreeTotals(
            files=summary.files,
            logical_bytes=summary.logical_bytes,
            allocated_bytes=summary.logical_bytes,
        )


def _is_rule_covered_whole_tree(root: KnownCleanupRoot) -> bool:
    rule = root.application_rule
    return (
        root.delete_root_itself
        and root.policy is CleanupPolicy.VENDOR_MANAGED
        and rule is not None
        and rule.owner is DecisionOwner.TOOL
        and rule.allow_whole_tree
    )


def _is_reachable(target: Path, roots: tuple[Path, ...]) -> bool:
    normalized = normalise_path(target)
    for root in roots:
        root_normalized = normalise_path(root)
        if normalized == root_normalized:
            return True
        if normalized.startswith(root_normalized.rstrip(os.sep) + os.sep):
            return True
    return False


def _identified_tree_summary(
    path: Path,
    cancel: CancellationToken,
) -> tuple[TreeSummary, FileSystemMetadata]:
    """Bind the displayed aggregate to the same ordinary directory before/after."""

    def read_root() -> FileSystemMetadata:
        metadata = read_file_metadata(path)
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or metadata.is_cloud_placeholder
            or metadata.volume_serial is None
            or metadata.file_id is None
            or metadata.file_id_kind != "file_id_128"
            or metadata.creation_time_ns is None
        ):
            raise TreeSummaryIncomplete("whole-tree root lacks a stable ordinary identity")
        return metadata

    try:
        before = read_root()
        summary = summarize_tree(path, cancel)
        after = read_root()
    except OSError as error:
        raise TreeSummaryIncomplete(str(error)) from error
    if before.identity != after.identity or before.creation_time_ns != after.creation_time_ns:
        raise TreeSummaryIncomplete("whole-tree root changed during summary")
    return summary, after


class ProductDevCleanWindow(ModernDevCleanWindow):
    """Single-path product UI with visible scan timing and hybrid traversal."""

    def __init__(self, root: tk.Tk) -> None:
        self._scan_started_at: float | None = None
        self._scan_clock_job: str | None = None
        self._scan_duration = tk.StringVar(master=root, value="扫描耗时：—")
        super().__init__(root)

    def _build_header(self, page: ttk.Frame) -> None:
        """Build the product header without a manual per-tool maintenance entry."""

        header = ttk.Frame(page, style="App.TFrame")
        header.pack(fill=tk.X, pady=(0, 14))
        header.columnconfigure(0, weight=1)

        brand = ttk.Frame(header, style="App.TFrame")
        brand.grid(row=0, column=0, sticky="w")
        ttk.Label(brand, text="DevClean", style="Title.TLabel").pack(anchor=tk.W)
        ttk.Label(
            brand,
            text="扫描一次，规则自动判断；本地规则无法确定的项目再进入 AI 复核",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))

        header_actions = ttk.Frame(header, style="App.TFrame")
        header_actions.grid(row=0, column=1, sticky="e")
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
        panel.columnconfigure(1, weight=1)

        range_box = ttk.Frame(panel, style="Surface.TFrame")
        range_box.grid(row=0, column=0, sticky="w")
        ttk.Label(range_box, text="扫描磁盘", style="Section.TLabel").pack(anchor=tk.W)
        drive_row = ttk.Frame(range_box, style="Surface.TFrame")
        drive_row.pack(anchor=tk.W, pady=(8, 0))
        preferred = app._system_drive()
        for drive in modern.ordered_drive_roots(fixed_volume_roots()):
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
                highlightbackground=modern._BORDER_STRONG,
                highlightcolor=modern._PRIMARY,
                background=modern._CONTROL,
                activebackground=modern._CONTROL_HOVER,
                selectcolor=modern._PRIMARY_SOFT,
                foreground=modern._TEXT,
                activeforeground=modern._PRIMARY,
                cursor="hand2",
                font=("Segoe UI Semibold", 9),
                padx=12,
                pady=6,
            )
            chip.pack(side=tk.LEFT, padx=(0, 7))

        explanation = ttk.Frame(panel, style="Surface.TFrame")
        explanation.grid(row=0, column=1, sticky="w", padx=(30, 24))
        ttk.Label(
            explanation,
            text="自动按规则扫描",
            style="Section.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            explanation,
            text="规则能决定的自动归类；已知整目录缓存快速汇总，只有未决内容才逐文件分析。",
            style="Body.TLabel",
        ).pack(anchor=tk.W, pady=(8, 0))

        action_box = ttk.Frame(panel, style="Surface.TFrame")
        action_box.grid(row=0, column=2, sticky="e")
        self._rescan = ttk.Button(
            action_box,
            text="开始扫描",
            style="Primary.TButton",
            command=self._start_scan,
        )
        self._rescan.pack(anchor=tk.E)

    def _build_status(self, page: ttk.Frame) -> None:
        super()._build_status(page)
        ttk.Label(
            page,
            textvariable=self._scan_duration,
            style="BodyOnPage.TLabel",
        ).pack(anchor=tk.E, pady=(0, 8))

    def _start_scan(self) -> None:
        self._cancel_scan_clock()
        self._scan_started_at = time.monotonic()
        self._scan_duration.set(_live_elapsed_text(0))
        try:
            self._rules = load_rules()
        except (OSError, RuleConfigError, UnicodeError) as error:
            self._scan_started_at = None
            self._scan_duration.set("扫描耗时：—")
            messagebox.showerror(
                "规则文件有误",
                f"{error}\n\n请点击“规则设置”修正后再扫描。",
            )
            return

        drives = tuple(drive for drive, state in self._drive_vars.items() if state.get())
        if not drives:
            self._scan_started_at = None
            self._scan_duration.set("扫描耗时：—")
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
        self._cleanup_failures.clear()
        self._cleanup_report = ""
        self._review_approvals.clear()
        self._ai_packages.clear()
        self._ai_group_members.clear()
        self._deletable_tree.delete(*self._deletable_tree.get_children())
        self._unsure_tree.delete(*self._unsure_tree.get_children())
        self._deletable_total.set("—")
        self._unsure_total.set("—")
        self._busy = "scanning"
        self._sync_buttons()
        self._progress.start(60)
        self._status.set("正在准备扫描…")
        self._scan_rules = self._rules

        # Every scan starts from fresh process state and tool discovery; nothing
        # cached by an earlier scan in this session is reused.
        clear_process_cache()
        self._known_roots = discover_known_cleanup_roots(self._scan_rules.scan)
        roots = app.scan_targets(self._known_roots, drives, self._scan_rules)
        if not roots:
            self._scan_started_at = None
            self._scan_duration.set("扫描耗时：—")
            self._progress.stop()
            self._busy = None
            self._sync_buttons()
            self._status.set("所选磁盘上没有可扫描的位置。")
            return

        self._status.set(f"正在扫描… 已规划 {len(roots):,} 个扫描根。")
        self._schedule_scan_clock(self._scan_token)
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

    # Application roots are discovered once for this whole scan instead of once
    # per file. Cleanup runs in a different worker and rediscovers them.
    @scan_root_snapshot()
    def _scan_worker(
        self,
        token: str,
        roots: tuple[Path, ...],
        cancel: CancellationToken,
        active_rules: UserRules,
        known_roots: tuple[KnownCleanupRoot, ...],
    ) -> None:
        session = _HybridTriageSession(
            review_sample_per_category=active_rules.scan.review_sample_per_category
        )
        now = datetime.now(UTC)
        active_known_roots = known_roots if active_rules.scan.include_known_cleanup_roots else ()
        atomic_roots = (
            tuple(
                root
                for root in active_known_roots
                if _is_rule_covered_whole_tree(root) and _is_reachable(root.path, roots)
            )
            if active_rules.scan.include_known_cleanup_roots
            else ()
        )
        atomic_paths = {normalise_path(root.path) for root in atomic_roots}

        configured_skip_paths = {
            normalise_path(path) for path in expanded_scan_paths(active_rules.scan.excluded_paths)
        }
        configured_skip_paths.add(normalise_path(data_dir()))
        configured_skip_paths.update(atomic_paths)
        if not active_rules.scan.include_known_cleanup_roots:
            configured_skip_paths.update(normalise_path(root.path) for root in known_roots)

        summarized_files = 0
        summarized_bytes = 0
        next_publish = time.monotonic() + app._SCAN_PREVIEW_INTERVAL_SECONDS
        final_stats: list[ScanStats] = []
        next_progress = 0.0
        temp_scan = TempFolderScan(
            active_known_roots,
            active_rules.keep.classification,
            active_rules,
            now=now,
        )

        try:
            for known in atomic_roots:
                if cancel.is_cancelled():
                    break
                try:
                    summary, metadata = _identified_tree_summary(known.path, cancel)
                except TreeSummaryIncomplete:
                    # The root itself: the whole item stays out.  Said once the
                    # scan is over, so nobody thinks it was looked at (a root
                    # that has meanwhile gone holds nothing to say about).
                    if not cancel.is_cancelled() and os.path.lexists(known.path):
                        session.unlisted_roots.append(str(known.path))
                    continue
                summarized_files += summary.files
                summarized_bytes += summary.logical_bytes
                self._events.put(("progress", (token, summarized_files, summarized_bytes)))

                evidence = WholeTreePolicyEvidence(
                    files=summary.files,
                    logical_bytes=summary.logical_bytes,
                    latest_activity_time_ns=summary.latest_activity_time_ns,
                )
                try:
                    eligible = assess_application_whole_tree_policy(
                        known.path,
                        active_known_roots,
                        evidence,
                    )
                except WholeTreePolicyRefusal:
                    continue
                if eligible is None:
                    continue

                record = ScanRecord(
                    root=str(known.path),
                    path=str(known.path),
                    kind=ScanRecordKind.DIRECTORY,
                    depth=0,
                    volume_serial=metadata.volume_serial,
                    file_id=metadata.file_id,
                    file_id_kind=metadata.file_id_kind,
                    link_count=metadata.link_count,
                    attributes=metadata.attributes,
                    reparse_tag=metadata.reparse_tag,
                    creation_time_ns=metadata.creation_time_ns,
                    last_write_time_ns=metadata.last_write_time_ns,
                )
                session.observe_path(
                    record.path,
                    active_rules,
                    target_kind=CleanupTargetKind.DIRECTORY,
                )
                item = triage_directory(
                    record,
                    known_roots=active_known_roots,
                    delete_config=active_rules.delete.classification,
                    keep_config=active_rules.keep.classification,
                )
                if item is not None:
                    note = skipped_entries_note(summary)
                    if note:
                        # What could not be read is left out; the rest is listed
                        # and cleaned as usual, and the row says what was left out.
                        item = replace(
                            item,
                            reason=f"{item.reason}；{note}",
                            tags=(*item.tags, "scan_skipped_entries"),
                        )
                        session.summary_notes[item.path] = note
                        session.summary_skipped += summary.skipped
                    session.add(item)
                    session.record_directory_summary(item.path, summary)
                if time.monotonic() >= next_publish:
                    buckets = app._rows_of(session, active_rules)
                    next_publish = time.monotonic() + app._SCAN_PREVIEW_INTERVAL_SECONDS
                    self._events.put(("scan_partial", (token, buckets)))

            def progress(stats: ScanStats) -> None:
                nonlocal next_progress
                final_stats[:] = [stats]
                moment = time.monotonic()
                # The scanner reports every few hundred entries; the window
                # only needs a few updates a second.
                if moment < next_progress and not stats.completed:
                    return
                next_progress = moment + app._PROGRESS_INTERVAL_SECONDS
                self._events.put(
                    (
                        "progress",
                        (
                            token,
                            summarized_files + stats.files,
                            summarized_bytes + stats.logical_bytes,
                        ),
                    )
                )

            for record in scan_roots(
                roots,
                ScanOptions(
                    include_directories=True,
                    exact_file_identity=False,
                    skip_directory_names=frozenset(
                        name.casefold() for name in active_rules.scan.skip_directory_names
                    ),
                    skip_paths=frozenset(configured_skip_paths),
                    root_skip_name_overrides=app._scan_root_skip_name_overrides(
                        roots,
                        known_roots,
                        active_rules,
                    ),
                ),
                cancel,
                progress,
            ):
                try:
                    if temp_scan.take(record):
                        # Inside a temporary folder: counted into its single
                        # item, not classified (see devclean.core.temp_folders).
                        pass
                    elif record.kind is ScanRecordKind.FILE:
                        session.observe_path(
                            record.path,
                            active_rules,
                            target_kind=CleanupTargetKind.FILE,
                        )
                        file_item = triage_file(
                            record,
                            known_roots=active_known_roots,
                            delete_config=active_rules.delete.classification,
                            keep_config=active_rules.keep.classification,
                            now=now,
                        )
                        routed = route_unresolved_file_to_ai(file_item)
                        session.add(app._effective_deletable_item(routed, active_rules))
                    elif record.kind is ScanRecordKind.DIRECTORY:
                        session.observe_path(
                            record.path,
                            active_rules,
                            target_kind=CleanupTargetKind.DIRECTORY,
                        )
                        item = triage_directory(
                            record,
                            known_roots=active_known_roots,
                            delete_config=active_rules.delete.classification,
                            keep_config=active_rules.keep.classification,
                        )
                        if item is not None:
                            session.add(item)
                except Exception:
                    # One file that cannot be classified is left out, which
                    # protects it, and is counted; it must not end the scan.
                    session.unclassified_files += 1
                if time.monotonic() >= next_publish:
                    buckets = app._rows_of(session, active_rules)
                    next_publish = time.monotonic() + app._SCAN_PREVIEW_INTERVAL_SECONDS
                    self._events.put(("scan_partial", (token, buckets)))
            for folder in temp_scan.folders:
                if not (folder.files or folder.directories):
                    continue
                temp_item = triage_temp_folder(
                    folder.record,
                    delete_config=active_rules.delete.classification,
                    kept_out=folder.kept_out,
                )
                session.add(temp_item)
                session.record_directory_summary(
                    temp_item.path,
                    TreeSummary(
                        files=folder.files,
                        logical_bytes=folder.logical_bytes,
                        latest_activity_time_ns=folder.latest_write_ns,
                    ),
                )
            walked = final_stats[0] if final_stats else None
            totals = app.ScanTotals(
                files=summarized_files + (walked.files if walked else 0),
                directories=len(atomic_roots) + (walked.directories if walked else 0),
                logical_bytes=summarized_bytes + (walked.logical_bytes if walked else 0),
            )
            # Partition and sort here, not on the UI thread.
            partition = app.published_partition(session, active_rules)
        except Exception as error:
            # Any failure is reported; a silently dead worker would leave the
            # window stuck in the scanning state.
            self._events.put(("scan_error", (token, f"{type(error).__name__}: {error}")))
            return
        self._events.put(
            ("scan_done", (token, session, cancel.is_cancelled(), totals, partition))
        )

    def _scan_finished(self, cancelled: bool, totals: app.ScanTotals | None) -> None:
        """Show the whole scan's totals and duration once its results are listed."""

        rendered = self._stop_scan_clock()
        skipped = getattr(self._session, "unclassified_files", 0)
        note = f"；{skipped:,} 个文件无法分类，已按保护处理、不列出" if skipped else ""
        if rendered is not None:
            label = "扫描已取消，用时" if cancelled else "扫描总耗时："
            self._scan_duration.set(f"{label}{rendered}{note}")
        summary = ""
        if totals is not None:
            summary = (
                f"共 {totals.files:,} 个文件、{totals.directories:,} 个文件夹，"
                f"总大小 {app._format_bytes(totals.logical_bytes)}"
            )
            if rendered is not None:
                summary += f"，总耗时 {rendered}"
            summary += "。"
        summary += _left_out_note(self._session)
        if cancelled:
            self._status.set(f"扫描已取消；已看过{summary}下面是已看到的部分。")
        else:
            self._status.set(
                f"扫描完成：{summary}"
                "左边是工具确定可删的；右边优先由你判断，真正未知的再交 AI。"
            )

    def _scan_failed(self) -> None:
        rendered = self._stop_scan_clock()
        self._scan_duration.set(
            "扫描耗时：—" if rendered is None else f"扫描耗时：{rendered}（扫描失败）"
        )

    def _schedule_scan_clock(self, token: str) -> None:
        """Show the real elapsed scan time, refreshed about once a second.

        One pending tick at most: a new scan cancels the previous one's.  Each
        tick only sets the label's text, and stops by itself once the clock is
        stopped or another scan has started.
        """

        self._cancel_scan_clock()
        self._scan_clock_job = self._root.after(
            _SCAN_CLOCK_INTERVAL_MS, self._tick_scan_clock, token
        )

    def _tick_scan_clock(self, token: str) -> None:
        self._scan_clock_job = None
        started = self._scan_started_at
        if started is None or token != self._scan_token:
            return
        self._scan_duration.set(_live_elapsed_text(time.monotonic() - started))
        self._scan_clock_job = self._root.after(
            _SCAN_CLOCK_INTERVAL_MS, self._tick_scan_clock, token
        )

    def _cancel_scan_clock(self) -> None:
        job = getattr(self, "_scan_clock_job", None)
        self._scan_clock_job = None
        if job is not None:
            with contextlib.suppress(tk.TclError):
                self._root.after_cancel(job)

    def _stop_scan_clock(self) -> str | None:
        """Stop the running scan clock and render it; ``None`` if none was running."""

        self._cancel_scan_clock()
        started = self._scan_started_at
        if started is None:
            return None
        self._scan_started_at = None
        elapsed = max(0.0, time.monotonic() - started)
        if elapsed < 60:
            return f"{elapsed:.1f} 秒"
        minutes, seconds = divmod(elapsed, 60)
        return f"{int(minutes)} 分 {seconds:.0f} 秒"


__all__ = ["ProductDevCleanWindow"]
