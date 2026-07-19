"""Tkinter workbench for scan -> classify -> review -> confirmed cleanup.

Scanning and AI import are observation-only.  Filesystem mutation exists only
behind a completed scan, an explicit local selection, a sealed internal batch,
and a final typed confirmation.  Model output is inert advice and never calls
the cleanup executor directly.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast
from uuid import uuid4

from devclean.core.ai_review_contract import (
    MAX_AI_RESPONSE_BYTES,
    AiRecommendation,
    AiReviewCandidateInput,
    AiReviewContractError,
    AiReviewImport,
    AiReviewPackage,
    build_ai_review_package,
    parse_ai_review_response,
    serialize_ai_review_package,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    KnownCleanupRoot,
    SourceDomain,
    discover_known_cleanup_roots,
    source_domain_for_category,
)
from devclean.core.cleanup_journal import (
    ActionState,
    CleanupJournal,
    CleanupMode,
)
from devclean.core.duplicates import DuplicateScanResult, find_large_duplicates
from devclean.core.paths import data_dir
from devclean.core.postscan_cleanup import (
    MAX_CLEANUP_PLAN_FILES,
    CleanupExecutionApproval,
    CleanupExecutionResult,
    CleanupRefusal,
    PreparedCleanupBatch,
    PreparedCleanupPlan,
    ScanCleanupCandidate,
    candidate_from_triage_item,
    confirm_cleanup_plan,
    execute_approved_batch,
    issue_cleanup_plan_confirmation,
    prepare_cleanup_plan,
    reconcile_unfinished_actions,
    restore_quarantined_action,
)
from devclean.core.reporting import write_report_stream
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
    TriageSession,
    triage_file,
)
from devclean.platform.windows.security import is_process_elevated
from devclean.platform.windows.volumes import is_local_fixed_path
from devclean.scanner import (
    CancellationToken,
    IncrementalScanSession,
    ScanOptions,
    ScanRecordKind,
    ScanStats,
    SessionScanMode,
    SessionScanStatus,
)

_FILTER_ALL = "全部"
_PLAN_DOCUMENT_TYPE = "DevClean_NON_EXECUTABLE_REVIEW_PLAN"
_PLAN_SCHEMA_VERSION = 1
_LANE_TITLES: Mapping[ReviewLane, str] = {
    ReviewLane.DETERMINISTIC_CANDIDATE: "确定性候选",
    ReviewLane.VENDOR_MANAGED: "厂商管理候选",
    ReviewLane.AI_REVIEW: "可选 AI / 人工复核",
    ReviewLane.REPORT_ONLY: "仅报告",
    ReviewLane.PROTECTED: "受保护",
}

_DOMAIN_TITLES: Mapping[SourceDomain, str] = {
    SourceDomain.AI_MODELS: "AI 模型与推理缓存",
    SourceDomain.PACKAGE_MANAGERS: "开发包管理缓存",
    SourceDomain.CONTAINERS_VIRTUALIZATION: "容器与虚拟化",
    SourceDomain.IDE_EDITORS: "IDE 与编辑器",
    SourceDomain.PROJECT_BUILD: "项目构建产物",
    SourceDomain.WINDOWS_SYSTEM: "Windows 与系统维护",
    SourceDomain.APPLICATION_CACHE: "应用缓存",
    SourceDomain.LOGS_DUMPS_TEMP: "日志、转储与临时文件",
    SourceDomain.INSTALLERS_DOWNLOADS: "安装包、下载与旧版本",
    SourceDomain.GENERAL_STORAGE: "通用空间分析",
}

_CATEGORY_TITLES: Mapping[CleanupCategory, str] = {
    CleanupCategory.USER_TEMP: "用户临时文件",
    CleanupCategory.CRASH_DUMPS: "崩溃转储",
    CleanupCategory.PIP_CACHE: "pip 缓存",
    CleanupCategory.UV_CACHE: "uv 缓存",
    CleanupCategory.NPM_CACHE: "npm 缓存",
    CleanupCategory.PNPM_STORE: "pnpm store",
    CleanupCategory.CONDA_CACHE: "Conda 缓存",
    CleanupCategory.HUGGINGFACE_CACHE: "Hugging Face 缓存",
    CleanupCategory.GRADLE_CACHE: "Gradle 缓存",
    CleanupCategory.YARN_CACHE: "Yarn 缓存",
    CleanupCategory.OLLAMA_MODELS: "Ollama 模型",
    CleanupCategory.VSCODE_CACHE: "VS Code 缓存",
    CleanupCategory.BROWSER_CACHE: "浏览器缓存",
    CleanupCategory.THUMBNAIL_CACHE: "缩略图缓存",
    CleanupCategory.CONTAINER_STORAGE: "容器存储",
    CleanupCategory.IDE_CACHE: "IDE 通用缓存",
    CleanupCategory.PROJECT_BUILD_OUTPUT: "项目构建产物",
    CleanupCategory.WINDOWS_UPDATE: "Windows 更新",
    CleanupCategory.SYSTEM_LOGS: "系统日志",
    CleanupCategory.INSTALLERS_DOWNLOADS: "安装包与下载",
    CleanupCategory.OTHER: "其它 / 未分类",
}

_RISK_TITLES: Mapping[RiskTier, str] = {
    RiskTier.LOW: "低",
    RiskTier.MEDIUM: "中",
    RiskTier.HIGH: "高",
    RiskTier.PROTECTED: "受保护",
}

_EVIDENCE_TITLES: Mapping[EvidenceKind, str] = {
    EvidenceKind.AGE_AND_APPROVED_ROOT: "已知根目录 + 年龄",
    EvidenceKind.KNOWN_ROOT_HEURISTIC: "已知根目录线索",
    EvidenceKind.PATH_HEURISTIC: "路径启发式",
    EvidenceKind.FILESYSTEM_OBSERVATION: "文件系统观察",
    EvidenceKind.PROTECTED_RULE: "硬保护规则",
}

_RECOVERY_TITLES: Mapping[RecoveryCapability, str] = {
    RecoveryCapability.UNKNOWN: "未知",
    RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT: "厂商重下（尽力）",
    RecoveryCapability.NONE: "无恢复承诺",
}

_EXECUTION_TITLES: Mapping[ExecutionPolicy, str] = {
    ExecutionPolicy.PERMANENT_APPROVED_CACHE: "批准缓存：可隔离 / 可永久清除",
    ExecutionPolicy.EXACT_VENDOR: "厂商精确动作",
    ExecutionPolicy.PREVIEWED_VENDOR: "厂商预览动作",
    ExecutionPolicy.POLICY_VENDOR: "厂商策略动作",
    ExecutionPolicy.RECYCLE_ONLY: "可隔离；永久清除需独立强确认",
    ExecutionPolicy.NONE: "不可执行",
}

_AI_RECOMMENDATION_TITLES: Mapping[AiRecommendation, str] = {
    AiRecommendation.KEEP: "AI 建议保留",
    AiRecommendation.RECOMMEND_RECYCLE: "AI 建议先安全隔离",
    AiRecommendation.UNSURE: "AI 不确定",
}


class WorkbenchState(StrEnum):
    """UI states; mutations are possible only in EXECUTING."""

    READY = "READY"
    SCANNING = "SCANNING"
    REVIEW = "REVIEW"
    EXECUTING = "EXECUTING"


def is_review_plan_eligible(item: TriageItem) -> bool:
    """Return whether a human may mark *item* for a non-executable plan."""

    return is_direct_cleanup_eligible(item)


def is_direct_cleanup_eligible(item: TriageItem) -> bool:
    """Return whether a human may explicitly select an executable file item.

    AI review is optional assistance.  It is not an authority gate: a user may
    directly select an uncertain but locally executable item, while protected,
    report-only, and unsupported vendor-action items remain blocked.
    """

    return (
        item.lane not in {ReviewLane.PROTECTED, ReviewLane.REPORT_ONLY}
        and item.actionability in {Actionability.REVIEW_PLAN, Actionability.AI_REVIEW}
        and item.execution_policy
        in {ExecutionPolicy.PERMANENT_APPROVED_CACHE, ExecutionPolicy.RECYCLE_ONLY}
        and item.risk_tier is not RiskTier.PROTECTED
    )


def is_low_risk_cleanup_eligible(item: TriageItem) -> bool:
    """Return whether deterministic policy allows low-risk bulk selection."""

    return (
        item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
        and item.actionability is Actionability.REVIEW_PLAN
        and item.execution_policy is ExecutionPolicy.PERMANENT_APPROVED_CACHE
        and item.risk_tier is RiskTier.LOW
    )


def is_ai_review_eligible(item: TriageItem) -> bool:
    """Return whether inert metadata may be exported for bounded AI advice."""

    return (
        item.lane is ReviewLane.AI_REVIEW
        and item.actionability is Actionability.AI_REVIEW
        and item.execution_policy is ExecutionPolicy.RECYCLE_ONLY
        and item.risk_tier is not RiskTier.PROTECTED
    )


def cleanup_mode_for_user_choice(
    candidates: Sequence[ScanCleanupCandidate], *, irreversible: bool
) -> CleanupMode:
    """Map an explicit final-page choice to the narrowest execution mode."""

    if not candidates:
        raise ValueError("cleanup mode requires at least one exact candidate")
    if not irreversible:
        return CleanupMode.RECYCLE
    if all(candidate.permanent_eligible for candidate in candidates):
        return CleanupMode.PERMANENT
    return CleanupMode.CONFIRMED_PURGE


def _ask_cleanup_mode_choice(
    parent: tk.Tk,
    *,
    file_count: int,
    total_bytes: int,
    permanent_count: int,
) -> bool | None:
    """Choose quarantine or irreversible purge; quarantine is the default.

    Returns ``False`` for recoverable quarantine, ``True`` for irreversible
    purge, and ``None`` for cancel.  The irreversible action is deliberately
    not the default button and never receives initial focus.
    """

    dialog = tk.Toplevel(parent)
    dialog.title("选择清理方式")
    dialog.transient(parent)
    dialog.grab_set()
    dialog.resizable(False, False)
    result: list[bool | None] = [None]

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        frame,
        text=(
            f"本次由你选择 {file_count} 个文件，共 {_format_bytes(total_bytes)}。"
        ),
        style="Section.TLabel",
        wraplength=640,
    ).pack(anchor=tk.W)
    ttk.Label(
        frame,
        text=(
            "· 仅隔离（默认，可恢复）：把同一文件精确移入 DevClean 私有隔离区，"
            "不释放该卷空间，可随时恢复。\n"
            "· 不可逆永久清除：先精确隔离并写入 PURGE_PENDING 意图，"
            "再从隔离区按句柄清除，之后无法恢复。\n\n"
            f"其中 {permanent_count} 个文件符合本机低风险缓存规则；其余文件即使有 "
            "AI 建议，也只有你的这次选择和下一页强确认才会授权永久清除。"
        ),
        style="Muted.TLabel",
        wraplength=640,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(8, 14))

    def choose(value: bool | None) -> None:
        result[0] = value
        dialog.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X)
    ttk.Button(
        buttons,
        text="不可逆永久清除…",
        style="Danger.TButton",
        command=lambda: choose(True),
    ).pack(side=tk.LEFT)
    ttk.Button(buttons, text="取消", command=lambda: choose(None)).pack(side=tk.RIGHT)
    quarantine = ttk.Button(
        buttons,
        text="仅移入私有隔离区（推荐）",
        style="Primary.TButton",
        command=lambda: choose(False),
    )
    quarantine.pack(side=tk.RIGHT, padx=(0, 8))
    dialog.protocol("WM_DELETE_WINDOW", lambda: choose(None))
    dialog.bind("<Escape>", lambda _event: choose(None))
    quarantine.bind("<Return>", lambda _event: choose(False))
    quarantine.focus_set()
    parent.wait_window(dialog)
    return result[0]


def _ask_typed_cleanup_confirmation(
    parent: tk.Tk,
    *,
    mode_title: str,
    warning: str,
    plan: PreparedCleanupPlan,
    phrase: str,
) -> str | None:
    """Show the exact immutable file manifest before collecting typed consent."""

    dialog = tk.Toplevel(parent)
    dialog.title("最终清理清单与强确认")
    dialog.geometry("900x650")
    dialog.minsize(720, 500)
    dialog.transient(parent)
    dialog.grab_set()
    result: list[str | None] = [None]

    frame = ttk.Frame(dialog, padding=16)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(
        frame,
        text=f"方式：{mode_title}",
        style="Title.TLabel",
    ).pack(anchor=tk.W)
    ttk.Label(
        frame,
        text=warning,
        style="Muted.TLabel",
        wraplength=840,
    ).pack(anchor=tk.W, pady=(6, 10))

    manifest = ScrolledText(frame, height=20, wrap=tk.NONE, font=("Consolas", 9))
    manifest.pack(fill=tk.BOTH, expand=True)
    manifest_lines = [
        f"plan_digest: {plan.digest}",
        f"journal_batches: {len(plan.batches)}",
        f"files: {len(plan.actions)}",
        "",
    ]
    manifest_lines.extend(
        f"{index:02d}. {_format_bytes(action.candidate.snapshot.logical_size):>12}  "
        f"{action.candidate.path}"
        for index, action in enumerate(plan.actions, start=1)
    )
    manifest.insert("1.0", "\n".join(manifest_lines))
    manifest.configure(state=tk.DISABLED)

    ttk.Label(
        frame,
        text=f"请完整输入：{phrase}",
        style="Section.TLabel",
        wraplength=840,
    ).pack(anchor=tk.W, pady=(12, 5))
    typed = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=typed)
    entry.pack(fill=tk.X)

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X, pady=(12, 0))

    def accept() -> None:
        result[0] = typed.get()
        dialog.destroy()

    def cancel() -> None:
        result[0] = None
        dialog.destroy()

    ttk.Button(buttons, text="取消", command=cancel).pack(side=tk.RIGHT)
    ttk.Button(
        buttons,
        text="确认此精确清单",
        style="Primary.TButton",
        command=accept,
    ).pack(side=tk.RIGHT, padx=(0, 8))
    dialog.protocol("WM_DELETE_WINDOW", cancel)
    entry.bind("<Return>", lambda _event: accept())
    entry.focus_set()
    parent.wait_window(dialog)
    return result[0]


def build_non_executable_review_plan(
    items: Sequence[TriageItem],
    *,
    scan_roots: Sequence[Path],
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Build a closed review-export shape with explicitly zero authority.

    This document is intentionally not compatible with an execution importer.
    It contains observations and human review marks, not actions or commands.
    """

    selected = tuple(items)
    if not selected:
        raise ValueError("at least one explicitly marked review candidate is required")
    if any(not is_review_plan_eligible(item) for item in selected):
        raise ValueError(
            "protected, report-only, or unsupported observations cannot enter this plan"
        )

    normalized_paths: set[str] = set()
    candidates: list[dict[str, object]] = []
    logical_bytes = 0
    allocated_bytes = 0
    allocation_unknown = 0
    for index, item in enumerate(selected, start=1):
        path_key = str(Path(item.path).absolute()).casefold()
        if path_key in normalized_paths:
            raise ValueError("duplicate review candidate path")
        normalized_paths.add(path_key)
        logical_bytes += item.logical_size
        if item.allocated_size is None:
            allocation_unknown += 1
        else:
            allocated_bytes += item.allocated_size
        record = item.record
        candidates.append(
            {
                "candidate_id": f"review_{index:04d}",
                "display_path": item.path,
                "source_domain": item.source_domain.value,
                "category": item.category.value,
                "review_lane": item.lane.value,
                "risk_tier": item.risk_tier.value,
                "evidence_kind": item.evidence_kind.value,
                "recovery_capability": item.recovery.value,
                "logical_size_bytes": item.logical_size,
                "allocated_size_bytes": item.allocated_size,
                "reason": item.reason,
                "tags": list(item.tags),
                "observational_snapshot": {
                    "valid_for_execution": False,
                    "volume_serial": record.volume_serial,
                    "file_id": record.file_id,
                    "file_id_kind": record.file_id_kind,
                    "creation_time_ns": record.creation_time_ns,
                    "last_write_time_ns": record.last_write_time_ns,
                },
            }
        )

    timestamp = created_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    return {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "document_type": _PLAN_DOCUMENT_TYPE,
        "execution_authority": "NONE",
        "import_contract": "UNSUPPORTED",
        "scan_complete": True,
        "selection_origin": "EXPLICIT_LOCAL_USER_MARKING",
        "default_selection_applied": False,
        "created_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "warning": (
            "Review export only. It grants no deletion, cleanup, command, elevation, "
            "or replay authority. DevClean does not import this document for execution."
        ),
        "scan_scope": [str(path) for path in scan_roots],
        "summary": {
            "candidate_count": len(candidates),
            "logical_size_bytes": logical_bytes,
            "known_allocated_size_bytes": allocated_bytes,
            "allocation_unknown_files": allocation_unknown,
        },
        "execution_actions": [],
        "review_candidates": candidates,
    }


def write_non_executable_review_plan(path: Path, plan: Mapping[str, object]) -> None:
    """Publish a plan as a new local file without overwriting a target."""

    if plan.get("document_type") != _PLAN_DOCUMENT_TYPE:
        raise ValueError("unexpected review document type")
    if plan.get("execution_authority") != "NONE":
        raise ValueError("review export must have zero execution authority")
    if plan.get("import_contract") != "UNSUPPORTED":
        raise ValueError("review export must not declare an import contract")
    if plan.get("execution_actions") != []:
        raise ValueError("review export cannot contain execution actions")
    rendered = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    write_report_stream(path, (rendered,))


class DevCleanWindow:
    """Native controlled-cleanup workbench with a read-only scan phase."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._root.title("DevClean · AI 与开发工具磁盘清理工作台")
        self._root.minsize(1050, 700)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._state = WorkbenchState.READY
        self._active_task = ""
        self._scan_complete = False
        self._session: TriageSession | None = None
        self._scan_cancel: CancellationToken | None = None
        self._duplicate_cancel: CancellationToken | None = None
        self._last_scan_roots: tuple[Path, ...] = ()
        self._last_scan_label = ""
        self._known_roots: tuple[KnownCleanupRoot, ...] = ()
        self._all_items: tuple[TriageItem, ...] = ()
        self._displayed_items: dict[str, TriageItem] = {}
        self._marked_ids: set[str] = set()
        self._ai_review_ids: set[str] = set()
        self._ai_package: AiReviewPackage | None = None
        self._ai_import: AiReviewImport | None = None
        self._ai_recommendations: dict[str, tuple[AiRecommendation, str]] = {}
        self._scan_session_id = ""
        self._active_scan_token = ""
        self._last_cleanup_results: tuple[CleanupExecutionResult, ...] = ()
        self._incremental_session: IncrementalScanSession | None = None
        self._quarantined_count = 0

        self._root_path = tk.StringVar(value=str(Path.home()))
        self._status = tk.StringVar(
            value="请选择本地固定磁盘上的目录。扫描阶段不会修改任何文件。"
        )
        self._step_text = tk.StringVar(
            value="1  扫描分类   →   2  选择   →   3  AI 复核   →   4  确认   →   5  删除验证"
        )
        self._safety_badge = tk.StringVar(value="扫描零副作用 · 删除需最终确认")
        self._domain_filter = tk.StringVar(value=_FILTER_ALL)
        self._lane_filter = tk.StringVar(value=_FILTER_ALL)
        self._search_filter = tk.StringVar()
        self._volume_note = tk.StringVar(value="完成扫描后显示所扫描驱动器的可用空间。")
        self._insight_note = tk.StringVar()
        self._display_cap_note = tk.StringVar()
        self._total_card = tk.StringVar(value="0")
        self._space_card = tk.StringVar(value="0 B")
        self._eligible_card = tk.StringVar(value="0")
        self._marked_card = tk.StringVar(value="0 项 · 0 B")

        self._result_tree: ttk.Treeview | None = None
        self._category_tree: ttk.Treeview | None = None
        self._directory_tree: ttk.Treeview | None = None
        self._duplicates_tree: ttk.Treeview | None = None
        self._details: ScrolledText | None = None
        self._scan_button: ttk.Button | None = None
        self._catalog_button: ttk.Button | None = None
        self._rescan_button: ttk.Button | None = None
        self._duplicate_button: ttk.Button | None = None
        self._cancel_button: ttk.Button | None = None
        self._mark_button: ttk.Button | None = None
        self._ai_mark_button: ttk.Button | None = None
        self._select_low_risk_button: ttk.Button | None = None
        self._select_filtered_button: ttk.Button | None = None
        self._clear_marks_button: ttk.Button | None = None
        self._export_button: ttk.Button | None = None
        self._import_ai_button: ttk.Button | None = None
        self._adopt_ai_button: ttk.Button | None = None
        self._execute_button: ttk.Button | None = None
        self._recovery_button: ttk.Button | None = None
        self._progress: ttk.Progressbar | None = None

        # Compatibility for older smoke tests; all lanes share the unified table.
        self._trees: dict[ReviewLane, ttk.Treeview] = {}

        self._build()
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_state(WorkbenchState.READY)
        self._root.after(80, self._drain_events)
        self._root.after(150, self._start_recovery_reconciliation)

    def _configure_style(self) -> None:
        self._root.configure(background="#f3f6fb")
        style = ttk.Style(self._root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("App.TFrame", background="#f3f6fb")
        style.configure("Panel.TFrame", background="#ffffff")
        style.configure("Panel.TLabelframe", background="#ffffff", borderwidth=1)
        style.configure("Panel.TLabelframe.Label", background="#ffffff", foreground="#22324a")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#5c6b80")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure(
            "Danger.TButton",
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground="#7f1d1d",
        )
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _on_close(self) -> None:
        if self._state is WorkbenchState.EXECUTING:
            messagebox.showwarning(
                "清理仍在执行",
                "请等待当前清理或恢复动作完成。强制终止仍会由 SQLite 意图日志标记为"
                "待复核，但当前窗口不会主动中断文件操作。",
                parent=self._root,
            )
            return
        if self._scan_cancel is not None:
            self._scan_cancel.cancel()
        if self._duplicate_cancel is not None:
            self._duplicate_cancel.cancel()
        session = self._incremental_session
        if session is not None and self._state is not WorkbenchState.SCANNING:
            session.close()
        self._root.destroy()

    @staticmethod
    def _cleanup_journal_path() -> Path:
        return data_dir() / "state" / "cleanup-intents-v1.db"

    def _start_recovery_reconciliation(self) -> None:
        """Observe unfinished durable actions at startup; never replay a mutation."""

        path = self._cleanup_journal_path()
        if not path.is_file():
            return
        threading.Thread(
            target=self._recovery_reconciliation_worker,
            args=(path,),
            daemon=True,
        ).start()

    def _recovery_reconciliation_worker(self, path: Path) -> None:
        try:
            journal = CleanupJournal(path)
            reconcile_unfinished_actions(journal)
            unresolved = journal.unresolved_actions()
            quarantined = sum(
                action.state is ActionState.QUARANTINED for action in unresolved
            )
            indeterminate = sum(
                action.state
                in {
                    ActionState.INTENT_RECORDED,
                    ActionState.EXECUTING,
                    ActionState.RECYCLE_PENDING,
                    ActionState.PURGE_PENDING,
                    ActionState.UNKNOWN,
                    ActionState.RESTORE_INTENT,
                    ActionState.RESTORING,
                }
                for action in unresolved
            )
        except Exception as error:
            self._events.put(("recovery_error", str(error)))
            return
        self._events.put(("recovery_state", (quarantined, indeterminate)))

    def _show_quarantine_manager(self) -> None:
        if self._state not in {WorkbenchState.READY, WorkbenchState.REVIEW}:
            return
        path = self._cleanup_journal_path()
        if not path.is_file():
            messagebox.showinfo("DevClean 隔离区", "当前没有持久化的隔离记录。")
            return
        try:
            journal = CleanupJournal(path)
            actions = tuple(
                action
                for action in journal.unresolved_actions()
                if action.state is ActionState.QUARANTINED
            )
        except Exception as error:
            messagebox.showerror("无法读取隔离区", str(error))
            return
        self._quarantined_count = len(actions)
        if not actions:
            messagebox.showinfo(
                "DevClean 隔离区",
                "没有可证明仍在私有隔离区中的文件。PURGE_PENDING 或不确定动作不会自动恢复。",
            )
            return
        preview = "\n".join(
            f"• {action.source_path}（{_format_bytes(action.snapshot.logical_size)}）"
            for action in actions[:12]
        )
        if len(actions) > 12:
            preview += f"\n…另有 {len(actions) - 12} 项"
        total = sum(action.snapshot.logical_size for action in actions)
        restore = messagebox.askyesno(
            "恢复私有隔离文件",
            (
                f"发现 {len(actions)} 个可恢复隔离文件，共 {_format_bytes(total)}。\n\n"
                f"{preview}\n\n"
                "选择“是”将逐项恢复到原路径；若原路径已被占用，该项会拒绝覆盖。"
            ),
            icon=messagebox.WARNING,
        )
        if not restore:
            return
        token = f"restore_{uuid4().hex}"
        self._active_task = token
        self._set_state(WorkbenchState.EXECUTING)
        self._status.set("正在按持久化身份逐项恢复私有隔离文件；不会覆盖已存在的原路径…")
        threading.Thread(
            target=self._restore_quarantine_worker,
            args=(token, path, tuple(action.action_id for action in actions)),
            daemon=True,
        ).start()

    def _restore_quarantine_worker(
        self, token: str, path: Path, action_ids: tuple[str, ...]
    ) -> None:
        restored: list[tuple[str, ActionState]] = []
        try:
            journal = CleanupJournal(path)
            for action_id in action_ids:
                restored.append(
                    (action_id, restore_quarantined_action(journal, action_id))
                )
        except Exception as error:
            self._events.put(("restore_error", (token, str(error), tuple(restored))))
            return
        self._events.put(("restore_finished", (token, tuple(restored))))

    def _build(self) -> None:
        self._configure_style()
        app = ttk.Frame(self._root, style="App.TFrame", padding=(18, 16))
        app.pack(fill=tk.BOTH, expand=True)

        hero = tk.Frame(app, background="#15263f", padx=20, pady=10)
        hero.pack(fill=tk.X)
        hero_left = tk.Frame(hero, background="#15263f")
        hero_left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            hero_left,
            text="DevClean",
            background="#15263f",
            foreground="#ffffff",
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            hero_left,
            text="扫描分类、AI 复核、人工确认与可审计删除",
            background="#15263f",
            foreground="#b9c8dc",
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor=tk.W, pady=(2, 0))
        tk.Label(
            hero,
            textvariable=self._safety_badge,
            background="#d7f5e9",
            foreground="#0b6b53",
            padx=12,
            pady=6,
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side=tk.RIGHT)

        stepper = tk.Label(
            app,
            textvariable=self._step_text,
            background="#e7eef9",
            foreground="#24476f",
            padx=14,
            pady=6,
            anchor=tk.W,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        stepper.pack(fill=tk.X, pady=(10, 10))

        controls = ttk.LabelFrame(
            app, text="扫描范围", style="Panel.TLabelframe", padding=(12, 7)
        )
        controls.pack(fill=tk.X)
        ttk.Label(controls, text="目录").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(controls, textvariable=self._root_path).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 8)
        )
        controls.columnconfigure(1, weight=1)
        ttk.Button(controls, text="选择…", command=self._choose_root).grid(row=0, column=2)
        scan_actions = ttk.Frame(controls, style="Panel.TFrame")
        scan_actions.grid(row=1, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        self._scan_button = ttk.Button(
            scan_actions,
            text="开始只读扫描",
            style="Primary.TButton",
            command=self._start_scan,
        )
        self._scan_button.pack(side=tk.LEFT)
        self._catalog_button = ttk.Button(
            scan_actions, text="盘点常见缓存", command=self._start_known_scan
        )
        self._catalog_button.pack(side=tk.LEFT, padx=(8, 0))
        self._rescan_button = ttk.Button(
            scan_actions, text="重新全量扫描", command=self._rescan_last_scan
        )
        self._rescan_button.pack(side=tk.LEFT, padx=(8, 0))
        self._cancel_button = ttk.Button(scan_actions, text="停止", command=self._cancel_scan)
        self._cancel_button.pack(side=tk.LEFT, padx=(8, 0))

        safety = tk.Label(
            app,
            text=(
                "安全边界：扫描、分类和 AI 导入阶段只读；扫描结束默认零选择；"
                "AI 只能给当前候选提建议；"
                "用户采用建议并最终确认后，执行器仍会复验批准根与文件身份。"
            ),
            background="#fff8dc",
            foreground="#6f5310",
            padx=12,
            pady=6,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=960,
        )
        safety.pack(fill=tk.X, pady=(8, 8))
        safety.bind(
            "<Configure>",
            lambda event: safety.configure(wraplength=max(500, event.width - 32)),
        )

        cards = ttk.Frame(app, style="App.TFrame")
        cards.pack(fill=tk.X, pady=(0, 8))
        for column, (title, variable, accent) in enumerate(
            (
                ("扫描文件", self._total_card, "#2b6cb0"),
                ("已分配空间", self._space_card, "#6b46c1"),
                ("可处理候选", self._eligible_card, "#0f766e"),
                ("待清理选择", self._marked_card, "#b45309"),
            )
        ):
            card = tk.Frame(
                cards,
                background="#ffffff",
                highlightthickness=1,
                highlightbackground="#dce4ef",
            )
            card.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 6, 0))
            cards.columnconfigure(column, weight=1)
            tk.Frame(card, background=accent, height=4).pack(fill=tk.X)
            tk.Label(
                card,
                text=title,
                background="#ffffff",
                foreground="#64748b",
                font=("Microsoft YaHei UI", 9),
            ).pack(anchor=tk.W, padx=12, pady=(6, 0))
            tk.Label(
                card,
                textvariable=variable,
                background="#ffffff",
                foreground="#172033",
                font=("Microsoft YaHei UI", 15, "bold"),
            ).pack(anchor=tk.W, padx=12, pady=(1, 6))

        notebook = ttk.Notebook(app)
        self._build_results_tab(notebook)
        self._build_overview_tab(notebook)
        self._build_duplicates_tab(notebook)

        actions = ttk.Frame(app, style="App.TFrame")
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        review_actions = ttk.Frame(actions, style="App.TFrame")
        review_actions.pack(fill=tk.X)
        execution_actions = ttk.Frame(actions, style="App.TFrame")
        execution_actions.pack(fill=tk.X, pady=(6, 0))
        self._mark_button = ttk.Button(
            review_actions,
            text="标记清理",
            command=self._toggle_selected_mark,
        )
        self._mark_button.pack(side=tk.LEFT)
        self._ai_mark_button = ttk.Button(
            review_actions, text="标记给 AI", command=self._toggle_selected_ai_review
        )
        self._ai_mark_button.pack(side=tk.LEFT, padx=(6, 0))
        self._select_low_risk_button = ttk.Button(
            review_actions, text="选择低风险项", command=self._select_low_risk_candidates
        )
        self._select_low_risk_button.pack(side=tk.LEFT, padx=(6, 0))
        self._select_filtered_button = ttk.Button(
            review_actions,
            text="选择当前筛选",
            command=self._select_filtered_candidates,
        )
        self._select_filtered_button.pack(side=tk.LEFT, padx=(6, 0))
        self._clear_marks_button = ttk.Button(
            review_actions, text="清空选择", command=self._clear_marks
        )
        self._clear_marks_button.pack(side=tk.LEFT, padx=(6, 0))
        self._export_button = ttk.Button(
            review_actions,
            text="导出 AI 复核包…",
            command=self._export_ai_review,
        )
        self._export_button.pack(side=tk.LEFT, padx=(6, 0))
        self._import_ai_button = ttk.Button(
            review_actions, text="导入 AI 建议…", command=self._import_ai_response
        )
        self._import_ai_button.pack(side=tk.LEFT, padx=(6, 0))
        self._adopt_ai_button = ttk.Button(
            review_actions, text="采用 AI 建议", command=self._adopt_ai_recommendations
        )
        self._adopt_ai_button.pack(side=tk.LEFT, padx=(6, 0))
        self._execute_button = ttk.Button(
            execution_actions,
            text="最终确认并删除…",
            style="Primary.TButton",
            command=self._confirm_and_execute_cleanup,
        )
        self._execute_button.pack(side=tk.RIGHT)
        self._duplicate_button = ttk.Button(
            execution_actions, text="只读重复文件分析", command=self._start_duplicate_scan
        )
        self._duplicate_button.pack(side=tk.LEFT)
        self._recovery_button = ttk.Button(
            execution_actions,
            text="隔离区 / 恢复…",
            command=self._show_quarantine_manager,
        )
        self._recovery_button.pack(side=tk.LEFT, padx=(6, 0))

        footer = ttk.Frame(app, style="App.TFrame")
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(9, 0))
        self._progress = ttk.Progressbar(footer, mode="indeterminate", length=170)
        self._progress.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            footer,
            textvariable=self._status,
            style="Muted.TLabel",
            wraplength=1000,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Pack the elastic notebook last so the fixed review actions and status
        # remain reachable on a 700/800 px-tall display.  Packing it earlier lets
        # its large tab contents consume the allocation and clips the controls.
        notebook.pack(fill=tk.BOTH, expand=True)

    def _build_results_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        notebook.add(frame, text="统一复核结果")
        filters = ttk.Frame(frame, style="Panel.TFrame")
        filters.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(filters, text="来源域").pack(side=tk.LEFT)
        ttk.Combobox(
            filters,
            textvariable=self._domain_filter,
            values=(_FILTER_ALL, *(_DOMAIN_TITLES[value] for value in SourceDomain)),
            state="readonly",
            width=22,
        ).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(filters, text="复核队列").pack(side=tk.LEFT)
        ttk.Combobox(
            filters,
            textvariable=self._lane_filter,
            values=(_FILTER_ALL, *(_LANE_TITLES[value] for value in ReviewLane)),
            state="readonly",
            width=16,
        ).pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(filters, text="搜索").pack(side=tk.LEFT)
        search = ttk.Entry(filters, textvariable=self._search_filter, width=34)
        search.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 8))
        search.bind("<Return>", lambda _event: self._apply_filters())
        ttk.Button(filters, text="应用筛选", command=self._apply_filters).pack(side=tk.LEFT)
        ttk.Button(filters, text="清除", command=self._clear_filters).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Label(
            frame,
            textvariable=self._display_cap_note,
            style="Muted.TLabel",
            wraplength=1200,
        ).pack(anchor=tk.W, pady=(0, 6))

        # Evidence details live beside the table.  A vertical split caused the
        # result rows to collapse on common 125%/150% DPI 720--800 px displays.
        pane = ttk.Panedwindow(frame, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True)
        table_frame = ttk.Frame(pane, style="Panel.TFrame")
        detail_frame = ttk.Frame(pane, style="Panel.TFrame")
        pane.add(table_frame, weight=4)
        pane.add(detail_frame, weight=1)

        columns = (
            "marked",
            "source",
            "category",
            "lane",
            "execution",
            "ai",
            "risk",
            "evidence",
            "recovery",
            "size",
            "reason",
            "path",
        )
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "marked": "计划",
            "source": "来源域",
            "category": "细分类",
            "lane": "复核队列",
            "execution": "执行上限",
            "ai": "AI 建议",
            "risk": "风险",
            "evidence": "证据",
            "recovery": "恢复能力",
            "size": "逻辑大小",
            "reason": "判定原因",
            "path": "路径",
        }
        widths = {
            "marked": 58,
            "source": 170,
            "category": 135,
            "lane": 115,
            "execution": 155,
            "ai": 105,
            "risk": 72,
            "evidence": 140,
            "recovery": 135,
            "size": 95,
            "reason": 300,
            "path": 520,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=widths[column],
                minwidth=50,
                stretch=column in {"reason", "path"},
                anchor=tk.E if column == "size" else tk.W,
            )
        vertical = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=tree.yview)
        horizontal = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky=tk.NSEW)
        vertical.grid(row=0, column=1, sticky=tk.NS)
        horizontal.grid(row=1, column=0, sticky=tk.EW)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        tree.tag_configure("marked", background="#e5f5ef")
        tree.tag_configure("ai_review", background="#eef2ff")
        tree.tag_configure("protected", background="#f5e9eb", foreground="#7f1d1d")
        tree.tag_configure("high", background="#fff6e5")
        tree.bind("<<TreeviewSelect>>", self._show_selected_details)
        tree.bind("<Double-1>", self._on_result_double_click)
        self._result_tree = tree
        self._trees = {lane: tree for lane in ReviewLane}

        ttk.Label(detail_frame, text="选中项证据详情", style="Section.TLabel").pack(
            anchor=tk.W, padx=(8, 0), pady=(0, 4)
        )
        details = ScrolledText(
            detail_frame,
            height=5,
            wrap=tk.WORD,
            borderwidth=1,
            relief=tk.SOLID,
            font=("Microsoft YaHei UI", 9),
        )
        details.pack(fill=tk.BOTH, expand=True, padx=(8, 0))
        details.configure(state=tk.DISABLED)
        self._details = details

    def _build_overview_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        notebook.add(frame, text="空间概览")
        ttk.Label(frame, textvariable=self._volume_note, style="Muted.TLabel").pack(
            anchor=tk.W, pady=(0, 8)
        )
        ttk.Label(frame, text="按来源域与细分类", style="Section.TLabel").pack(anchor=tk.W)
        self._category_tree = ttk.Treeview(
            frame,
            columns=("source", "category", "files", "logical", "allocated"),
            show="headings",
            height=8,
        )
        for column, title, width in (
            ("source", "来源域", 240),
            ("category", "细分类", 180),
            ("files", "文件数", 100),
            ("logical", "逻辑大小", 130),
            ("allocated", "已分配空间", 130),
        ):
            self._category_tree.heading(column, text=title)
            self._category_tree.column(
                column,
                width=width,
                anchor=tk.E if column in {"files", "logical", "allocated"} else tk.W,
            )
        self._category_tree.pack(fill=tk.X, pady=(5, 12))
        ttk.Label(frame, text="扫描根目录下占用最多的位置", style="Section.TLabel").pack(
            anchor=tk.W
        )
        self._directory_tree = ttk.Treeview(
            frame, columns=("path", "files", "logical", "allocated"), show="headings"
        )
        for column, title, width in (
            ("path", "目录", 700),
            ("files", "文件数", 100),
            ("logical", "逻辑大小", 130),
            ("allocated", "已分配空间", 130),
        ):
            self._directory_tree.heading(column, text=title)
            self._directory_tree.column(
                column, width=width, anchor=tk.E if column != "path" else tk.W
            )
        self._directory_tree.pack(fill=tk.BOTH, expand=True, pady=(5, 4))
        ttk.Label(frame, textvariable=self._insight_note, style="Muted.TLabel").pack(anchor=tk.W)

    def _build_duplicates_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook, style="Panel.TFrame", padding=10)
        notebook.add(frame, text="重复文件（只读）")
        ttk.Label(
            frame,
            text=(
                "独立只读分析：仅对至少 1 MiB 的稳定普通文件计算 SHA-256。每个重复组完整展示，"
                "不会指定正本、不会标记副本，也不会把结果自动加入复核计划。"
            ),
            wraplength=1080,
            style="Muted.TLabel",
        ).pack(fill=tk.X, pady=(0, 8))
        tree = ttk.Treeview(
            frame,
            columns=("copies", "file_size", "duplicate_bytes", "digest", "path"),
            show="tree headings",
        )
        tree.heading("#0", text="重复组 / 文件")
        tree.column("#0", width=170)
        for column, title, width in (
            ("copies", "文件数", 80),
            ("file_size", "单文件大小", 120),
            ("duplicate_bytes", "理论重复量（非计划）", 160),
            ("digest", "SHA-256", 190),
            ("path", "路径 / 说明", 680),
        ):
            tree.heading(column, text=title)
            tree.column(
                column,
                width=width,
                anchor=tk.E if column in {"copies", "file_size", "duplicate_bytes"} else tk.W,
            )
        tree.pack(fill=tk.BOTH, expand=True)
        self._duplicates_tree = tree

    def _set_state(self, state: WorkbenchState) -> None:
        self._state = state
        scanning = state is WorkbenchState.SCANNING
        executing = state is WorkbenchState.EXECUTING
        busy = scanning or executing
        reviewing = state is WorkbenchState.REVIEW and self._scan_complete
        for button in (
            self._scan_button,
            self._catalog_button,
            self._rescan_button,
            self._duplicate_button,
        ):
            if button is not None:
                button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if self._rescan_button is not None and not self._last_scan_roots:
            self._rescan_button.configure(state=tk.DISABLED)
        if self._rescan_button is not None:
            incremental_ready = bool(
                self._incremental_session is not None
                and self._incremental_session.has_baseline
                and self._incremental_session.incremental_ready
            )
            self._rescan_button.configure(
                text=("同会话增量刷新" if incremental_ready else "重新全量扫描")
            )
        if self._duplicate_button is not None and not reviewing:
            self._duplicate_button.configure(state=tk.DISABLED)
        if self._cancel_button is not None:
            self._cancel_button.configure(state=tk.NORMAL if scanning else tk.DISABLED)
        if self._progress is not None:
            if busy:
                self._progress.start(12)
            else:
                self._progress.stop()
        if scanning:
            self._step_text.set(
                "1  扫描分类中   →   2  选择（锁定）   →   3  AI（锁定）"
                "   →   4  确认   →   5  删除"
            )
            self._safety_badge.set("只读扫描中 · 操作锁定")
        elif executing:
            self._step_text.set(
                "1  已扫描   →   2  已选择   →   3  已复核   →   4  已确认   →   5  执行验证中"
            )
            self._safety_badge.set("删除执行中 · 计划已锁定")
        elif reviewing:
            self._step_text.set(
                "1  扫描完成   →   2  选择   →   3  可选 AI 复核   →   "
                "4  最终确认   →   5  删除验证"
            )
            self._safety_badge.set("等待选择 · 默认零选择")
        else:
            self._step_text.set(
                "1  扫描分类   →   2  选择   →   3  可选 AI 复核   →   4  确认   →   5  删除验证"
            )
            self._safety_badge.set("扫描零副作用 · 删除需最终确认")
        self._refresh_action_states()

    def _refresh_action_states(self) -> None:
        reviewing = self._state is WorkbenchState.REVIEW and self._scan_complete
        selected_item = self._selected_item()
        can_mark = (
            reviewing
            and selected_item is not None
            and is_direct_cleanup_eligible(selected_item)
        )
        if self._mark_button is not None:
            self._mark_button.configure(state=tk.NORMAL if can_mark else tk.DISABLED)
        if self._ai_mark_button is not None:
            self._ai_mark_button.configure(
                state=(
                    tk.NORMAL
                    if reviewing
                    and selected_item is not None
                    and is_ai_review_eligible(selected_item)
                    else tk.DISABLED
                )
            )
        if self._select_low_risk_button is not None:
            self._select_low_risk_button.configure(
                state=(
                    tk.NORMAL
                    if reviewing
                    and any(is_low_risk_cleanup_eligible(item) for item in self._all_items)
                    else tk.DISABLED
                )
            )
        if self._select_filtered_button is not None:
            self._select_filtered_button.configure(
                state=(
                    tk.NORMAL
                    if reviewing
                    and any(
                        is_direct_cleanup_eligible(item) and self._matches_filters(item)
                        for item in self._all_items
                    )
                    else tk.DISABLED
                )
            )
        if self._clear_marks_button is not None:
            self._clear_marks_button.configure(
                state=(
                    tk.NORMAL
                    if reviewing and (self._marked_ids or self._ai_review_ids)
                    else tk.DISABLED
                )
            )
        if self._export_button is not None:
            self._export_button.configure(
                state=tk.NORMAL if reviewing and self._ai_review_ids else tk.DISABLED
            )
        if self._import_ai_button is not None:
            self._import_ai_button.configure(
                state=tk.NORMAL if reviewing and self._ai_package is not None else tk.DISABLED
            )
        if self._adopt_ai_button is not None:
            has_recommendation = any(
                recommendation is AiRecommendation.RECOMMEND_RECYCLE
                for recommendation, _reason in self._ai_recommendations.values()
            )
            self._adopt_ai_button.configure(
                state=tk.NORMAL if reviewing and has_recommendation else tk.DISABLED
            )
        if self._execute_button is not None:
            self._execute_button.configure(
                state=tk.NORMAL if reviewing and self._marked_ids else tk.DISABLED
            )
        if self._recovery_button is not None:
            self._recovery_button.configure(
                state=(
                    tk.NORMAL
                    if self._state in {WorkbenchState.READY, WorkbenchState.REVIEW}
                    else tk.DISABLED
                )
            )

    def _choose_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self._root_path.get() or str(Path.home()))
        if selected:
            self._root_path.set(selected)

    def _start_scan(self) -> None:
        root = Path(self._root_path.get()).expanduser()
        if not root.is_dir():
            messagebox.showerror("DevClean", "请选择一个存在的目录。")
            return
        if not is_local_fixed_path(root):
            messagebox.showerror("DevClean", "只允许扫描本地固定磁盘且不经过重解析点的目录。")
            return
        self._begin_scan((root,), scan_label="所选目录")

    def _start_known_scan(self) -> None:
        known_roots = discover_known_cleanup_roots()
        roots = tuple(item.path for item in known_roots)
        if not roots:
            messagebox.showinfo("DevClean", "没有发现可盘点的本地常见缓存目录。")
            return
        self._begin_scan(roots, scan_label=f"{len(roots)} 个常见缓存目录")

    def _rescan_last_scan(self) -> None:
        if not self._last_scan_roots:
            messagebox.showinfo("DevClean", "还没有可重新扫描的范围。")
            return
        if (
            self._incremental_session is not None
            and self._incremental_session.has_baseline
            and self._incremental_session.incremental_ready
        ):
            self._begin_incremental_refresh(scan_label=f"增量刷新：{self._last_scan_label}")
            return
        self._begin_scan(
            self._last_scan_roots,
            scan_label=f"重新全量扫描：{self._last_scan_label}",
        )

    def _begin_scan(self, roots: tuple[Path, ...], *, scan_label: str) -> None:
        if self._incremental_session is not None:
            self._incremental_session.close()
        incremental_session = IncrementalScanSession(
            roots,
            ScanOptions(include_directories=False),
        )
        self._incremental_session = incremental_session
        scan_token = f"scan_{uuid4().hex}"
        self._active_scan_token = scan_token
        self._scan_session_id = ""
        self._scan_complete = False
        self._session = None
        self._all_items = ()
        self._displayed_items.clear()
        self._marked_ids.clear()
        self._ai_review_ids.clear()
        self._ai_package = None
        self._ai_import = None
        self._ai_recommendations.clear()
        self._clear_result_views()
        self._last_scan_roots = roots
        self._last_scan_label = scan_label
        self._known_roots = discover_known_cleanup_roots()
        cancel = CancellationToken()
        self._scan_cancel = cancel
        self._active_task = "scan"
        self._set_state(WorkbenchState.SCANNING)
        self._status.set(
            f"正在只读扫描{scan_label}；不会删除、移动、清理、调用 AI 或执行外部命令…"
        )
        threading.Thread(
            target=self._scan_worker,
            args=(scan_token, incremental_session, False, cancel, self._known_roots),
            daemon=True,
        ).start()

    def _begin_incremental_refresh(self, *, scan_label: str) -> None:
        incremental_session = self._incremental_session
        if incremental_session is None or not incremental_session.has_baseline:
            self._begin_scan(self._last_scan_roots, scan_label="全量回退")
            return
        scan_token = f"scan_{uuid4().hex}"
        self._active_scan_token = scan_token
        self._scan_session_id = ""
        self._scan_complete = False
        self._marked_ids.clear()
        self._ai_review_ids.clear()
        self._ai_package = None
        self._ai_import = None
        self._ai_recommendations.clear()
        cancel = CancellationToken()
        self._scan_cancel = cancel
        self._active_task = "scan"
        self._set_state(WorkbenchState.SCANNING)
        self._status.set(
            f"正在{scan_label}；仅重扫变化的保守父目录。监视失效时会自动回退全量…"
        )
        threading.Thread(
            target=self._scan_worker,
            args=(scan_token, incremental_session, True, cancel, self._known_roots),
            daemon=True,
        ).start()

    def _cancel_scan(self) -> None:
        token = self._scan_cancel or self._duplicate_cancel
        if token is None:
            return
        token.cancel()
        if self._cancel_button is not None:
            self._cancel_button.configure(state=tk.DISABLED)
        self._status.set("正在停止只读任务；未完成结果不会获得标记或导出权限。")

    def _scan_worker(
        self,
        scan_token: str,
        incremental_session: IncrementalScanSession,
        refresh: bool,
        cancel: CancellationToken,
        known_roots: tuple[KnownCleanupRoot, ...],
    ) -> None:
        def progress(stats: ScanStats) -> None:
            self._events.put(
                (
                    "scan_progress",
                    (
                        scan_token,
                        stats.files,
                        stats.logical_bytes,
                        stats.allocated_bytes,
                        stats.errors,
                        stats.boundaries,
                    ),
                )
            )

        try:
            result = (
                incremental_session.refresh(cancel=cancel, progress=progress)
                if refresh
                else incremental_session.baseline(cancel=cancel, progress=progress)
            )
        except (OSError, RuntimeError, ValueError) as error:
            self._events.put(("scan_error", (scan_token, str(error))))
            return
        if result.status is SessionScanStatus.FAILED:
            self._events.put(
                ("scan_error", (scan_token, result.error or "增量扫描协调器失败"))
            )
            return

        session = TriageSession()
        observed = 0
        logical_bytes = 0
        allocated_bytes = 0
        errors = 0
        boundaries = 0
        temp_root = Path(tempfile.gettempdir())
        for record in result.records:
            if record.kind is ScanRecordKind.ERROR:
                errors += 1
                continue
            if record.kind is ScanRecordKind.BOUNDARY:
                boundaries += 1
                continue
            if record.kind is not ScanRecordKind.FILE:
                continue
            item = triage_file(record, temp_root=temp_root, known_roots=known_roots)
            session.add(item)
            observed += 1
            logical_bytes += record.logical_size
            if record.allocated_size is not None:
                allocated_bytes += record.allocated_size
        fallback_text = "; ".join(report.code for report in result.fallbacks[:4])
        self._events.put(
            (
                "scan_finished",
                (
                    scan_token,
                    session,
                    observed,
                    logical_bytes,
                    allocated_bytes,
                    errors,
                    boundaries,
                    result.status is SessionScanStatus.CANCELLED,
                    result.mode,
                    result.stats.incremental_ready,
                    result.stats.records_reobserved,
                    result.stats.records_reused,
                    fallback_text,
                ),
            )
        )

    def _start_duplicate_scan(self) -> None:
        if not self._scan_complete or not self._last_scan_roots:
            messagebox.showinfo("DevClean", "请先完成一次只读扫描。")
            return
        if self._state is WorkbenchState.SCANNING:
            return
        cancel = CancellationToken()
        self._duplicate_cancel = cancel
        self._active_task = "duplicates"
        self._set_state(WorkbenchState.SCANNING)
        self._status.set("正在进行只读重复分析；人工标记和导出暂时锁定…")
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

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "scan_progress":
                    token, observed, logical, allocated, errors, boundaries = cast(
                        tuple[str, int, int, int, int, int], payload
                    )
                    if token != self._active_scan_token:
                        continue
                    self._total_card.set(f"{observed:,}")
                    self._space_card.set(_format_bytes(allocated))
                    self._status.set(
                        f"只读扫描中：{observed:,} 个文件，逻辑 {_format_bytes(logical)}，"
                        f"已分配 {_format_bytes(allocated)}；错误 {errors}，边界 {boundaries}…"
                    )
                elif kind == "scan_finished":
                    (
                        token,
                        session,
                        observed,
                        logical,
                        allocated,
                        errors,
                        boundaries,
                        cancelled,
                        scan_mode,
                        incremental_ready,
                        records_reobserved,
                        records_reused,
                        fallback_text,
                    ) = cast(
                        tuple[
                            str,
                            TriageSession,
                            int,
                            int,
                            int,
                            int,
                            int,
                            bool,
                            SessionScanMode,
                            bool,
                            int,
                            int,
                            str,
                        ],
                        payload,
                    )
                    if token != self._active_scan_token:
                        continue
                    self._scan_cancel = None
                    self._active_task = ""
                    self._session = session
                    self._render_session(session)
                    if cancelled:
                        self._scan_complete = False
                        self._scan_session_id = ""
                        self._marked_ids.clear()
                        self._set_state(WorkbenchState.READY)
                        self._status.set(
                            f"扫描已停止：展示 {observed:,} 个只读观察；"
                            "结果不完整，不能标记或导出。"
                        )
                    else:
                        self._scan_complete = True
                        self._scan_session_id = token
                        self._marked_ids.clear()
                        self._set_state(WorkbenchState.REVIEW)
                        if scan_mode is SessionScanMode.INCREMENTAL:
                            mode_text = (
                                f"增量刷新完成：重观察 {records_reobserved:,} 条，"
                                f"复用 {records_reused:,} 条"
                            )
                        elif scan_mode is SessionScanMode.FALLBACK:
                            mode_text = "监视状态失效，已安全回退全量扫描"
                        else:
                            mode_text = "全量基线完成"
                        monitor_text = (
                            "同会话增量已就绪"
                            if incremental_ready
                            else "下次刷新仍需全量扫描"
                        )
                        fallback_suffix = (
                            f"；回退原因 {fallback_text}" if fallback_text else ""
                        )
                        self._status.set(
                            f"{mode_text}：{observed:,} 个文件，逻辑 {_format_bytes(logical)}，"
                            f"已分配 {_format_bytes(allocated)}；错误 {errors}，边界 {boundaries}；"
                            f"{monitor_text}{fallback_suffix}；默认零选择。"
                        )
                elif kind == "duplicates":
                    result = cast(DuplicateScanResult, payload)
                    self._duplicate_cancel = None
                    self._active_task = ""
                    self._render_duplicate_groups(result)
                    self._set_state(WorkbenchState.REVIEW)
                    qualifiers: list[str] = []
                    if result.truncated:
                        qualifiers.append("结果受边界限制")
                    if result.cancelled:
                        qualifiers.append("任务已停止")
                    suffix = f"；{'；'.join(qualifiers)}" if qualifiers else ""
                    self._status.set(
                        f"重复分析完成：{len(result.groups)} 组，已哈希 "
                        f"{result.files_hashed:,} 个文件；没有自动指定或标记任何副本{suffix}。"
                    )
                elif kind == "duplicate_error":
                    self._duplicate_cancel = None
                    self._active_task = ""
                    self._set_state(WorkbenchState.REVIEW)
                    messagebox.showerror("重复文件分析失败", str(payload))
                elif kind == "recovery_state":
                    quarantined, indeterminate = cast(tuple[int, int], payload)
                    self._quarantined_count = quarantined
                    self._refresh_action_states()
                    if self._state is WorkbenchState.READY and (
                        quarantined or indeterminate
                    ):
                        self._status.set(
                            f"启动恢复检查：可恢复隔离 {quarantined} 项，"
                            f"待人工判定 {indeterminate} 项；不会自动重放。"
                        )
                elif kind == "recovery_error":
                    if self._state is WorkbenchState.READY:
                        self._status.set(
                            f"持久化隔离日志需要人工检查：{payload}"
                        )
                elif kind == "restore_finished":
                    token, restored = cast(
                        tuple[str, tuple[tuple[str, ActionState], ...]], payload
                    )
                    if token != self._active_task:
                        continue
                    self._active_task = ""
                    self._scan_complete = False
                    self._marked_ids.clear()
                    self._ai_review_ids.clear()
                    restored_count = sum(
                        state is ActionState.RESTORED for _action_id, state in restored
                    )
                    review_count = len(restored) - restored_count
                    self._quarantined_count = max(
                        0, self._quarantined_count - restored_count
                    )
                    self._set_state(WorkbenchState.READY)
                    messagebox.showinfo(
                        "隔离恢复结果",
                        f"已恢复 {restored_count} 项；仍需复核 {review_count} 项。"
                        "原路径被占用时不会覆盖。",
                    )
                    self._start_recovery_reconciliation()
                    if self._last_scan_roots:
                        self._begin_scan(
                            self._last_scan_roots,
                            scan_label=f"恢复后核验：{self._last_scan_label}",
                        )
                elif kind == "restore_error":
                    token, detail, restored = cast(
                        tuple[
                            str,
                            str,
                            tuple[tuple[str, ActionState], ...],
                        ],
                        payload,
                    )
                    if token != self._active_task:
                        continue
                    self._active_task = ""
                    self._scan_complete = False
                    self._marked_ids.clear()
                    self._ai_review_ids.clear()
                    self._set_state(WorkbenchState.READY)
                    messagebox.showerror(
                        "隔离恢复需要复核",
                        f"{detail}\n\n已产生状态结果 {len(restored)} 项；不会自动重试。",
                    )
                    self._start_recovery_reconciliation()
                elif kind == "cleanup_finished":
                    token, results = cast(
                        tuple[str, tuple[CleanupExecutionResult, ...]], payload
                    )
                    if token != self._active_task:
                        continue
                    self._active_task = ""
                    self._last_cleanup_results = results
                    self._scan_complete = False
                    self._marked_ids.clear()
                    self._ai_review_ids.clear()
                    self._set_state(WorkbenchState.READY)
                    messagebox.showinfo("清理执行结果", _cleanup_result_summary(results))
                    if self._last_scan_roots:
                        if (
                            self._incremental_session is not None
                            and self._incremental_session.has_baseline
                        ):
                            self._begin_incremental_refresh(scan_label="清理后增量核验")
                        else:
                            self._begin_scan(
                                self._last_scan_roots,
                                scan_label=f"清理后核验：{self._last_scan_label}",
                            )
                elif kind == "cleanup_error":
                    token, detail, results = cast(
                        tuple[str, str, tuple[CleanupExecutionResult, ...]], payload
                    )
                    if token != self._active_task:
                        continue
                    self._active_task = ""
                    self._last_cleanup_results = results
                    self._scan_complete = False
                    self._marked_ids.clear()
                    self._ai_review_ids.clear()
                    self._set_state(WorkbenchState.READY)
                    summary = _cleanup_result_summary(results) if results else "尚无完整批次结果。"
                    messagebox.showerror(
                        "清理执行需要复核",
                        f"{detail}\n\n{summary}\n\n不会自动重放未知动作；将重新扫描当前范围。",
                    )
                    if self._last_scan_roots:
                        if (
                            self._incremental_session is not None
                            and self._incremental_session.has_baseline
                        ):
                            self._begin_incremental_refresh(scan_label="异常后增量核验")
                        else:
                            self._begin_scan(
                                self._last_scan_roots,
                                scan_label=f"异常后核验：{self._last_scan_label}",
                            )
                elif kind == "scan_error":
                    token, detail = cast(tuple[str, str], payload)
                    if token != self._active_scan_token:
                        continue
                    self._scan_cancel = None
                    self._active_task = ""
                    self._scan_complete = False
                    self._scan_session_id = ""
                    self._marked_ids.clear()
                    self._set_state(WorkbenchState.READY)
                    messagebox.showerror("DevClean", f"只读扫描失败：{detail}")
        except queue.Empty:
            pass
        self._root.after(80, self._drain_events)

    def _clear_result_views(self) -> None:
        if self._result_tree is not None:
            self._result_tree.delete(*self._result_tree.get_children())
        if self._category_tree is not None:
            self._category_tree.delete(*self._category_tree.get_children())
        if self._directory_tree is not None:
            self._directory_tree.delete(*self._directory_tree.get_children())
        if self._duplicates_tree is not None:
            self._duplicates_tree.delete(*self._duplicates_tree.get_children())
        self._set_details("选择结果后查看完整证据与安全边界。")
        self._display_cap_note.set("")
        self._total_card.set("0")
        self._space_card.set("0 B")
        self._eligible_card.set("0")
        self._marked_card.set("0 项 · 0 B")

    def _render_session(self, session: TriageSession) -> None:
        self._all_items = session.all_display_items()
        self._displayed_items = {
            f"item:{index:04d}": item for index, item in enumerate(self._all_items)
        }
        self._marked_ids.clear()
        self._apply_filters()
        self._render_insights(session)
        self._render_volume_summary()
        total_files = sum(session.summary(lane).files for lane in ReviewLane)
        total_allocated = sum(session.summary(lane).allocated_bytes for lane in ReviewLane)
        eligible = sum(
            session.summary(lane).files
            for lane in (ReviewLane.DETERMINISTIC_CANDIDATE, ReviewLane.AI_REVIEW)
        )
        self._total_card.set(f"{total_files:,}")
        self._space_card.set(_format_bytes(total_allocated))
        self._eligible_card.set(f"{eligible:,}")
        displayed = len(self._all_items)
        if total_files > displayed:
            self._display_cap_note.set(
                f"表格按队列仅保留最大的 {session.display_limit} 个候选："
                f"当前可见 {displayed:,} 项，扫描总计 {total_files:,} 个文件；"
                "未显示的文件不会被选择或删除，分类汇总与统计始终完整。"
            )
        else:
            self._display_cap_note.set(f"表格已显示全部 {displayed:,} 个扫描文件。")
        self._update_marked_card()

    def _apply_filters(self) -> None:
        tree = self._result_tree
        if tree is None:
            return
        selected_before = tree.selection()
        tree.delete(*tree.get_children())
        items = [
            (item_id, item)
            for item_id, item in self._displayed_items.items()
            if self._matches_filters(item)
        ]
        items.sort(key=lambda pair: pair[1].logical_size, reverse=True)
        for item_id, item in items:
            recommendation = self._ai_recommendations.get(_path_key(item.path))
            tags: tuple[str, ...]
            if item_id in self._marked_ids:
                tags = ("marked",)
            elif item_id in self._ai_review_ids:
                tags = ("ai_review",)
            elif item.lane is ReviewLane.PROTECTED:
                tags = ("protected",)
            elif item.risk_tier is RiskTier.HIGH:
                tags = ("high",)
            else:
                tags = ()
            if item_id in self._marked_ids:
                mark_glyph = "☑ 清理"
            elif item_id in self._ai_review_ids:
                mark_glyph = "AI 复核"
            elif is_direct_cleanup_eligible(item):
                mark_glyph = "☐"
            elif is_ai_review_eligible(item):
                mark_glyph = "AI?"
            elif item.lane is ReviewLane.PROTECTED:
                mark_glyph = "锁定"
            else:
                mark_glyph = "—"
            tree.insert(
                "",
                tk.END,
                iid=item_id,
                values=(
                    mark_glyph,
                    _DOMAIN_TITLES[item.source_domain],
                    _CATEGORY_TITLES[item.category],
                    _LANE_TITLES[item.lane],
                    _EXECUTION_TITLES[item.execution_policy],
                    (
                        _AI_RECOMMENDATION_TITLES[recommendation[0]]
                        if recommendation is not None
                        else "—"
                    ),
                    _RISK_TITLES[item.risk_tier],
                    _EVIDENCE_TITLES[item.evidence_kind],
                    _RECOVERY_TITLES[item.recovery],
                    _format_bytes(item.logical_size),
                    item.reason,
                    item.path,
                ),
                tags=tags,
            )
        if selected_before and tree.exists(selected_before[0]):
            tree.selection_set(selected_before[0])
        self._refresh_action_states()

    def _matches_filters(self, item: TriageItem) -> bool:
        domain = self._domain_filter.get()
        if domain != _FILTER_ALL and domain != _DOMAIN_TITLES[item.source_domain]:
            return False
        lane = self._lane_filter.get()
        if lane != _FILTER_ALL and lane != _LANE_TITLES[item.lane]:
            return False
        query = self._search_filter.get().strip().casefold()
        if not query:
            return True
        haystack = " ".join(
            (
                item.path,
                item.reason,
                _DOMAIN_TITLES[item.source_domain],
                _CATEGORY_TITLES[item.category],
                _LANE_TITLES[item.lane],
                " ".join(item.tags),
            )
        ).casefold()
        return query in haystack

    def _clear_filters(self) -> None:
        self._domain_filter.set(_FILTER_ALL)
        self._lane_filter.set(_FILTER_ALL)
        self._search_filter.set("")
        self._apply_filters()

    def _selected_item(self) -> TriageItem | None:
        tree = self._result_tree
        if tree is None:
            return None
        selection = tree.selection()
        return self._displayed_items.get(selection[0]) if selection else None

    def _selected_item_id(self) -> str | None:
        tree = self._result_tree
        if tree is None:
            return None
        selection = tree.selection()
        return selection[0] if selection else None

    def _show_selected_details(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        item = self._selected_item()
        if item is None:
            self._set_details("选择结果后查看完整证据与安全边界。")
        else:
            if is_direct_cleanup_eligible(item):
                eligible = (
                    "可由用户直接加入受控清理计划；AI 复核是可选辅助"
                    if is_ai_review_eligible(item)
                    else "可由用户加入受控清理计划"
                )
            elif is_ai_review_eligible(item):
                eligible = "可导出给 AI 复核；导回后仍需用户采用"
            else:
                eligible = "当前执行策略不可清理"
            recommendation = self._ai_recommendations.get(_path_key(item.path))
            allocated = (
                _format_bytes(item.allocated_size)
                if item.allocated_size is not None
                else "未知 / 估算不可用"
            )
            self._set_details(
                "\n".join(
                    (
                        f"安全状态：{eligible}",
                        f"来源域：{_DOMAIN_TITLES[item.source_domain]}",
                        f"细分类：{_CATEGORY_TITLES[item.category]}",
                        f"复核队列：{_LANE_TITLES[item.lane]}    "
                        f"风险：{_RISK_TITLES[item.risk_tier]}",
                        f"证据：{_EVIDENCE_TITLES[item.evidence_kind]}",
                        f"本机执行上限：{_EXECUTION_TITLES[item.execution_policy]}",
                        (
                            "AI 建议：尚未导入"
                            if recommendation is None
                            else f"AI 建议：{_AI_RECOMMENDATION_TITLES[recommendation[0]]}；"
                            f"理由：{recommendation[1]}"
                        ),
                        f"恢复能力：{_RECOVERY_TITLES[item.recovery]}",
                        f"逻辑大小：{_format_bytes(item.logical_size)}    已分配：{allocated}",
                        f"标签：{', '.join(item.tags) if item.tags else '无'}",
                        f"原因：{item.reason}",
                        f"路径：{item.path}",
                    )
                )
            )
        self._refresh_action_states()

    def _set_details(self, value: str) -> None:
        if self._details is None:
            return
        self._details.configure(state=tk.NORMAL)
        self._details.delete("1.0", tk.END)
        self._details.insert("1.0", value)
        self._details.configure(state=tk.DISABLED)

    def _on_result_double_click(self, event: tk.Event[tk.Misc]) -> None:
        tree = self._result_tree
        if tree is None:
            return
        item_id = tree.identify_row(event.y)
        if item_id:
            tree.selection_set(item_id)
            item = self._displayed_items.get(item_id)
            if item is not None and is_direct_cleanup_eligible(item):
                self._toggle_selected_mark()
            elif item is not None and is_ai_review_eligible(item):
                self._toggle_selected_ai_review()

    def _toggle_selected_mark(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        item_id = self._selected_item_id()
        item = self._displayed_items.get(item_id or "")
        if item_id is None or item is None:
            return
        if not is_direct_cleanup_eligible(item):
            self._status.set(
                "该项属于受保护、只报告或尚未实现的执行类型，不能加入清理。"
            )
            return
        if item_id in self._marked_ids:
            self._marked_ids.remove(item_id)
            verb = "取消标记"
        else:
            if len(self._marked_ids) >= MAX_CLEANUP_PLAN_FILES:
                self._status.set(
                    f"单次精确计划最多 {MAX_CLEANUP_PLAN_FILES} 个文件；"
                    "执行器会自动拆成可审计小批次。"
                )
                return
            self._marked_ids.add(item_id)
            verb = "已人工标记"
        self._apply_filters()
        if self._result_tree is not None and self._result_tree.exists(item_id):
            self._result_tree.selection_set(item_id)
        self._update_marked_card()
        self._status.set(f"{verb}：{item.path}。这不会修改文件。")

    def _toggle_selected_ai_review(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        item_id = self._selected_item_id()
        item = self._displayed_items.get(item_id or "")
        if item_id is None or item is None or not is_ai_review_eligible(item):
            self._status.set("该项不属于可导出的 AI 复核队列。")
            return
        if item_id in self._ai_review_ids:
            self._ai_review_ids.remove(item_id)
            verb = "已取消 AI 复核标记"
        else:
            self._ai_review_ids.add(item_id)
            verb = "已加入 AI 复核"
        self._apply_filters()
        if self._result_tree is not None and self._result_tree.exists(item_id):
            self._result_tree.selection_set(item_id)
        self._status.set(f"{verb}：{item.path}。此时仍没有任何删除权限。")

    def _select_low_risk_candidates(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        available = [
            item_id
            for item_id, item in self._displayed_items.items()
            if is_low_risk_cleanup_eligible(item)
        ]
        remaining = MAX_CLEANUP_PLAN_FILES - len(self._marked_ids)
        selected = [item_id for item_id in available if item_id not in self._marked_ids][
            :remaining
        ]
        self._marked_ids.update(selected)
        self._apply_filters()
        self._update_marked_card()
        suffix = (
            f"；单次计划上限为 {MAX_CLEANUP_PLAN_FILES}，其余未选择"
            if len(available) > len(selected)
            else ""
        )
        self._status.set(
            f"已由用户操作选择 {len(selected)} 个低风险确定性候选{suffix}；尚未删除。"
        )

    def _select_filtered_candidates(self) -> None:
        """Explicitly select actionable rows visible under the current filters."""

        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        available = [
            item_id
            for item_id, item in self._displayed_items.items()
            if self._matches_filters(item)
            and is_direct_cleanup_eligible(item)
            and item_id not in self._marked_ids
        ]
        remaining = MAX_CLEANUP_PLAN_FILES - len(self._marked_ids)
        selected = available[:remaining]
        self._marked_ids.update(selected)
        high_risk = sum(
            self._displayed_items[item_id].risk_tier is RiskTier.HIGH
            for item_id in selected
        )
        self._apply_filters()
        self._update_marked_card()
        suffix = (
            f"；另有 {len(available) - len(selected)} 项因计划上限未选择"
            if len(available) > len(selected)
            else ""
        )
        self._status.set(
            f"已由用户操作选择当前筛选中的 {len(selected)} 项，其中高风险 "
            f"{high_risk} 项{suffix}；尚未删除，仍需精确清单和最终确认。"
        )

    def _clear_marks(self) -> None:
        if self._state is not WorkbenchState.REVIEW:
            return
        self._marked_ids.clear()
        self._ai_review_ids.clear()
        self._apply_filters()
        self._update_marked_card()
        self._status.set("已清空清理选择与 AI 复核标记；没有修改任何文件。")

    def _marked_items(self) -> tuple[TriageItem, ...]:
        return tuple(
            self._displayed_items[item_id]
            for item_id in sorted(self._marked_ids)
            if item_id in self._displayed_items
        )

    def _update_marked_card(self) -> None:
        items = self._marked_items()
        total = sum(item.logical_size for item in items)
        self._marked_card.set(f"{len(items):,} 项 · {_format_bytes(total)}")
        self._refresh_action_states()

    def _ai_review_items(self) -> tuple[TriageItem, ...]:
        return tuple(
            self._displayed_items[item_id]
            for item_id in sorted(self._ai_review_ids)
            if item_id in self._displayed_items
        )

    def _export_ai_review(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        items = self._ai_review_items()
        if not items:
            messagebox.showinfo("DevClean", "请先标记至少一个待 AI 复核项。")
            return
        if not self._scan_session_id:
            messagebox.showerror("DevClean", "当前扫描会话无效，请重新扫描。")
            return
        try:
            package = build_ai_review_package(
                tuple(
                    AiReviewCandidateInput(item=item, hard_protected=False)
                    for item in items
                ),
                scan_session_id=self._scan_session_id,
            )
            rendered = serialize_ai_review_package(package) + "\n"
        except (AiReviewContractError, TypeError, ValueError) as error:
            messagebox.showerror("AI 复核包创建失败", str(error))
            return
        suggested = f"DevClean-ai-review-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
        selected = filedialog.asksaveasfilename(
            title="导出 AI 复核包",
            defaultextension=".json",
            initialfile=suggested,
            filetypes=(("JSON", "*.json"),),
            confirmoverwrite=False,
        )
        if not selected:
            return
        destination = Path(selected)
        try:
            write_report_stream(destination, (rendered,))
        except (FileExistsError, OSError, TypeError, ValueError) as error:
            messagebox.showerror("AI 复核包导出失败", str(error))
            return
        self._ai_package = package
        self._ai_import = None
        self._ai_recommendations.clear()
        self._apply_filters()
        self._status.set(
            f"已导出 {len(items)} 项 AI 复核包：{destination}。"
            "包内只有脱敏元数据，execution_authority=NONE。"
        )

    def _import_ai_response(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        package = self._ai_package
        if package is None:
            messagebox.showinfo("DevClean", "请先从当前扫描会话导出 AI 复核包。")
            return
        selected = filedialog.askopenfilename(
            title="导入 AI 建议响应",
            filetypes=(("JSON", "*.json"), ("所有文件", "*.*")),
        )
        if not selected:
            return
        source = Path(selected)
        try:
            with source.open("rb") as response_file:
                raw_response = response_file.read(MAX_AI_RESPONSE_BYTES + 1)
            if len(raw_response) > MAX_AI_RESPONSE_BYTES:
                raise AiReviewContractError("AI response exceeds the byte limit")
            imported = parse_ai_review_response(
                raw_response.decode("utf-8", errors="strict"), package
            )
        except (AiReviewContractError, OSError, UnicodeError, ValueError) as error:
            messagebox.showerror("AI 建议导入失败", str(error))
            return
        self._ai_import = imported
        self._ai_recommendations = {
            _path_key(recommendation.item.path): (
                recommendation.recommendation,
                recommendation.reason,
            )
            for recommendation in imported.recommendations
        }
        self._apply_filters()
        recommended = sum(
            recommendation.recommendation is AiRecommendation.RECOMMEND_RECYCLE
            for recommendation in imported.recommendations
        )
        self._status.set(
            f"已验证并导入 {len(imported.recommendations)} 条 AI 建议，"
            f"其中 {recommended} 条建议回收。"
            "导入没有自动选择，也没有调用删除执行器。"
        )

    def _adopt_ai_recommendations(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        if self._ai_import is None:
            return
        available = [
            item_id
            for item_id, item in self._displayed_items.items()
            if is_ai_review_eligible(item)
            and self._ai_recommendations.get(_path_key(item.path), (None, ""))[0]
            is AiRecommendation.RECOMMEND_RECYCLE
            and item_id not in self._marked_ids
        ]
        remaining = MAX_CLEANUP_PLAN_FILES - len(self._marked_ids)
        adopted = available[:remaining]
        self._marked_ids.update(adopted)
        self._ai_review_ids.difference_update(adopted)
        self._apply_filters()
        self._update_marked_card()
        suffix = (
            f"；另有 {len(available) - len(adopted)} 条因单批上限未采用"
            if len(available) > len(adopted)
            else ""
        )
        self._status.set(
            f"用户已显式采用 {len(adopted)} 条 AI 安全移除建议{suffix}。"
            "AI 建议本身只建议先隔离；是否永久清除由你在最终页独立授权。"
        )

    def _confirm_and_execute_cleanup(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        items = self._marked_items()
        if not items:
            return
        try:
            candidates = tuple(
                candidate_from_triage_item(item, known_roots=self._known_roots)
                for item in items
            )
        except (CleanupRefusal, OSError, RuntimeError, TypeError, ValueError) as error:
            messagebox.showerror("无法生成清理计划", str(error))
            return

        permanent = tuple(candidate for candidate in candidates if candidate.permanent_eligible)
        answer = _ask_cleanup_mode_choice(
            self._root,
            file_count=len(candidates),
            total_bytes=sum(item.logical_size for item in items),
            permanent_count=len(permanent),
        )
        if answer is None:
            return
        mode = cleanup_mode_for_user_choice(candidates, irreversible=answer)

        try:
            plan = prepare_cleanup_plan(candidates)
        except (CleanupRefusal, TypeError, ValueError) as error:
            messagebox.showerror("无法生成清理计划", str(error))
            return

        try:
            challenge = issue_cleanup_plan_confirmation(plan, mode=mode)
        except CleanupRefusal as error:
            messagebox.showerror("最终确认失败", str(error))
            return
        impact = sum(action.candidate.snapshot.logical_size for action in plan.actions)
        irreversible = mode in {
            CleanupMode.PERMANENT,
            CleanupMode.CONFIRMED_PURGE,
        }
        warning = (
            "这是不可恢复的永久清除：先精确隔离，再写不可逆意图并按句柄清除。"
            if irreversible
            else "文件只移入 DevClean 私有隔离区；可恢复，但不会释放该卷空间。"
        )
        mode_title = "永久清除" if irreversible else "可恢复私有隔离"
        typed = _ask_typed_cleanup_confirmation(
            self._root,
            mode_title=(
                f"{mode_title} · {len(plan.actions)} 个文件 · "
                f"{_format_bytes(impact)} · {len(plan.batches)} 个日志批次"
            ),
            warning=warning,
            plan=plan,
            phrase=challenge.phrase,
        )
        if typed is None:
            return
        try:
            approvals = confirm_cleanup_plan(plan, challenge, typed)
        except CleanupRefusal as error:
            messagebox.showerror("确认文本不匹配", str(error))
            return
        confirmed = tuple(zip(plan.batches, approvals, strict=True))

        execution_token = f"cleanup_{uuid4().hex}"
        self._active_task = execution_token
        self._set_state(WorkbenchState.EXECUTING)
        self._status.set(
            "最终计划已锁定；正在逐项复验文件身份、先写 SQLite 意图，再执行删除…"
        )
        threading.Thread(
            target=self._cleanup_worker,
            args=(execution_token, confirmed),
            daemon=True,
        ).start()

    def _cleanup_worker(
        self,
        execution_token: str,
        confirmed: tuple[tuple[PreparedCleanupBatch, CleanupExecutionApproval], ...],
    ) -> None:
        results: list[CleanupExecutionResult] = []
        try:
            for batch, approval in confirmed:
                result = execute_approved_batch(batch, approval)
                results.append(result)
                expected = (
                    ActionState.QUARANTINED
                    if approval.mode is CleanupMode.RECYCLE
                    else ActionState.PURGED
                )
                if any(state is not expected for _action_id, state in result.action_states):
                    break
        except Exception as error:
            self._events.put(("cleanup_error", (execution_token, str(error), tuple(results))))
            return
        self._events.put(("cleanup_finished", (execution_token, tuple(results))))

    def _export_review_plan(self) -> None:
        if self._state is not WorkbenchState.REVIEW or not self._scan_complete:
            return
        items = self._marked_items()
        if not items:
            messagebox.showinfo("DevClean", "请先人工标记至少一个允许复核的候选项。")
            return
        if any(not is_review_plan_eligible(item) for item in items):
            messagebox.showerror("DevClean", "标记集合包含不可加入计划的项目，已拒绝导出。")
            return
        suggested = f"DevClean-review-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
        selected = filedialog.asksaveasfilename(
            title="导出不可执行复核计划",
            defaultextension=".json",
            initialfile=suggested,
            filetypes=(("JSON", "*.json"),),
            confirmoverwrite=False,
        )
        if not selected:
            return
        destination = Path(selected)
        try:
            plan = build_non_executable_review_plan(items, scan_roots=self._last_scan_roots)
            write_non_executable_review_plan(destination, plan)
        except (FileExistsError, OSError, TypeError, ValueError) as error:
            messagebox.showerror("导出失败", str(error))
            return
        self._status.set(
            f"已导出 {len(items)} 项不可执行复核计划：{destination}；没有修改扫描文件。"
        )
        messagebox.showinfo(
            "导出完成",
            "复核计划已作为新文件写出。execution_authority=NONE，且不支持导入执行。",
        )

    def _render_insights(self, session: TriageSession) -> None:
        if self._category_tree is None or self._directory_tree is None:
            return
        self._category_tree.delete(*self._category_tree.get_children())
        self._directory_tree.delete(*self._directory_tree.get_children())
        for category, summary in session.insights.category_items():
            self._category_tree.insert(
                "",
                tk.END,
                values=(
                    _DOMAIN_TITLES[source_domain_for_category(category)],
                    _CATEGORY_TITLES[category],
                    f"{summary.files:,}",
                    _format_bytes(summary.logical_bytes),
                    _format_bytes(summary.allocated_bytes),
                ),
            )
        for insight in session.insights.top_directories():
            self._directory_tree.insert(
                "",
                tk.END,
                values=(
                    insight.path,
                    f"{insight.summary.files:,}",
                    _format_bytes(insight.summary.logical_bytes),
                    _format_bytes(insight.summary.allocated_bytes),
                ),
            )
        self._insight_note.set(
            "目录桶超过 2,000 个，概览已明确截断；分类总计仍完整。"
            if session.insights.skipped_directory_buckets
            else "目录概览按扫描根目录的首层聚合；分类总计始终完整。"
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

    def _render_duplicate_groups(self, result: DuplicateScanResult) -> None:
        tree = self._duplicates_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for index, group in enumerate(result.groups, start=1):
            group_id = f"duplicate:{index}"
            tree.insert(
                "",
                tk.END,
                iid=group_id,
                text=f"重复组 {index}",
                values=(
                    len(group.records),
                    _format_bytes(group.logical_size),
                    _format_bytes(group.reclaimable_logical_bytes),
                    group.digest,
                    "只读分组；未指定正本或待处理副本",
                ),
                open=False,
            )
            for file_index, record in enumerate(group.records, start=1):
                tree.insert(
                    group_id,
                    tk.END,
                    text=f"文件 {file_index}",
                    values=("", "", "", "", record.path),
                )


def _path_key(value: str) -> str:
    return str(Path(value).absolute()).casefold()


def _cleanup_result_summary(results: Sequence[CleanupExecutionResult]) -> str:
    if not results:
        return "没有执行任何清理批次。"
    lines: list[str] = []
    purged_logical = 0
    for index, result in enumerate(results, start=1):
        states = [state for _action_id, state in result.action_states]
        purged = sum(state is ActionState.PURGED for state in states)
        unchanged = sum(state is ActionState.FAILED_UNCHANGED for state in states)
        recoverable = sum(state is ActionState.QUARANTINED for state in states)
        unknown = sum(state is ActionState.UNKNOWN for state in states)
        expected = (
            ActionState.QUARANTINED
            if result.mode is CleanupMode.RECYCLE
            else ActionState.PURGED
        )
        expected_complete = bool(states) and all(state is expected for state in states)
        purged_logical += result.purged_logical_bytes
        mode = (
            "永久清除"
            if result.mode in {CleanupMode.PERMANENT, CleanupMode.CONFIRMED_PURGE}
            else "可恢复私有隔离"
        )
        if expected_complete:
            state = "已隔离并可恢复" if expected is ActionState.QUARANTINED else "完成"
        elif unknown:
            state = "需要人工复核"
        else:
            state = "已停止 / 部分未执行"
        lines.append(
            f"批次 {index}（{mode}）：{state}；已永久清除 {purged}，"
            f"可恢复隔离 {recoverable}，未改动 {unchanged}，不确定 {unknown}。"
        )
    lines.append(
        f"已验证永久移除文件的逻辑大小：{_format_bytes(purged_logical)}。"
        "这不是卷空闲空间的实测值；稀疏、压缩及仍打开的句柄会影响实际释放。"
    )
    if any(result.mode is CleanupMode.RECYCLE for result in results):
        lines.append("私有隔离与源文件位于同一卷，因此隔离本身不释放空间。")
    lines.append(
        "执行记录已写入本机 SQLite 意图日志；PURGE_PENDING/不确定状态不会自动重放。"
    )
    return "\n".join(lines)


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    raise AssertionError("unreachable")


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
    try:
        elevated = is_process_elevated()
    except OSError:
        elevated = True
    if elevated:
        warning = tk.Tk()
        warning.withdraw()
        messagebox.showerror(
            "DevClean",
            "DevClean 主程序拒绝以管理员权限运行。请关闭后用普通用户方式启动。",
            parent=warning,
        )
        warning.destroy()
        return 2
    root = tk.Tk()
    DevCleanWindow(root)
    root.mainloop()
    return 0


__all__ = [
    "DevCleanWindow",
    "WorkbenchState",
    "build_non_executable_review_plan",
    "cleanup_mode_for_user_choice",
    "is_ai_review_eligible",
    "is_direct_cleanup_eligible",
    "is_low_risk_cleanup_eligible",
    "is_review_plan_eligible",
    "main",
    "write_non_executable_review_plan",
]
