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

import os
import threading
import time
import tkinter as tk
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk
from uuid import uuid4

from devclean.core.application_cleanup import DecisionOwner
from devclean.core.cleanup_catalog import (
    CleanupPolicy,
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.paths import data_dir
from devclean.core.review_routing import route_unresolved_file_to_ai
from devclean.core.triage import (
    CleanupTargetKind,
    DirectorySubtreeTotals,
    TriageSession,
    triage_directory,
    triage_file,
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
    TreeSummary,
    TreeSummaryIncomplete,
    summarize_tree,
)
from devclean.ui import app
from devclean.ui import modern_app as modern
from devclean.ui.modern_app import ModernDevCleanWindow


class _HybridTriageSession(TriageSession):
    """Triage session that can accept an aggregate for a known directory."""

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


class ProductDevCleanWindow(ModernDevCleanWindow):
    """Single-path product UI with visible scan timing and hybrid traversal."""

    def __init__(self, root: tk.Tk) -> None:
        self._scan_started_at: float | None = None
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
        self._scan_started_at = time.monotonic()
        self._scan_duration.set("扫描耗时：计时中…")
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

        try:
            for known in atomic_roots:
                if cancel.is_cancelled():
                    break
                try:
                    summary = summarize_tree(known.path, cancel)
                except TreeSummaryIncomplete:
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
                    last_write_time_ns=summary.latest_activity_time_ns,
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
                    session.add(item)
                    session.record_directory_summary(item.path, summary)
                if time.monotonic() >= next_publish:
                    buckets = app._rows_of(session, active_rules)
                    next_publish = time.monotonic() + app._SCAN_PREVIEW_INTERVAL_SECONDS
                    self._events.put(("scan_partial", (token, buckets)))

            def progress(stats: ScanStats) -> None:
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
                if record.kind is ScanRecordKind.FILE:
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
                if time.monotonic() >= next_publish:
                    buckets = app._rows_of(session, active_rules)
                    next_publish = time.monotonic() + app._SCAN_PREVIEW_INTERVAL_SECONDS
                    self._events.put(("scan_partial", (token, buckets)))
        except (OSError, RuntimeError, ValueError) as error:
            self._events.put(("scan_error", (token, str(error))))
            return
        self._events.put(("scan_done", (token, session, cancel.is_cancelled())))

    def _publish(self, session: TriageSession) -> None:
        super()._publish(session)
        started = self._scan_started_at
        if started is None:
            return
        elapsed = max(0.0, time.monotonic() - started)
        self._scan_started_at = None
        if elapsed < 60:
            rendered = f"{elapsed:.1f} 秒"
        else:
            minutes, seconds = divmod(elapsed, 60)
            rendered = f"{int(minutes)} 分 {seconds:.0f} 秒"
        self._scan_duration.set(f"扫描耗时：{rendered}")


__all__ = ["ProductDevCleanWindow"]
