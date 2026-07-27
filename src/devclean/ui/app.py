"""DevClean GUI: scan selected fixed drives, sort into two buckets, delete.

Two buckets, because the tool has exactly two answers about any file: it is sure
the file can go, or it is not sure and the question goes to a model.  The
three public rule files define where to scan and what to delete or keep.  Only
items the model explicitly leaves UNSURE can be decided by the user in the same
right-hand bucket; there is no third queue.

Mutation lives in ``core.postscan_cleanup`` and nothing here can widen it.
"""

# Chinese UI prose uses fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import contextlib
import heapq
import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Sequence
from dataclasses import dataclass
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
    AiReviewImport,
    AiReviewPackage,
    AiReviewSimilarityGroup,
    build_ai_review_package,
    parse_ai_review_response,
    parse_partial_ai_review_response,
    serialize_ai_review_package,
)
from devclean.core.cleanup_catalog import (
    KnownCleanupRoot,
    discover_known_cleanup_roots,
)
from devclean.core.cleanup_journal import ActionState, CleanupMode
from devclean.core.paths import data_dir
from devclean.core.postscan_cleanup import (
    CleanupExecutionProgress,
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
    reusable_path_pattern,
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
# Rows drawn in the table.  The scan rules separately bound the largest
# candidates retained per classification for review and AI export; this limit
# only bounds how many lines tkinter has to lay out, because
# rebuilding tens of thousands of rows every 1.5 seconds during a scan makes
# the window unusable.  The header states the real count next to it.
_ROWS_DRAWN = 2_000
# A live preview is rebuilt repeatedly and only needs enough rows to fill the
# visible pane.  The completed scan still draws ``_ROWS_DRAWN`` rows from the
# configured review sample.
_LIVE_ROWS_DRAWN = 300
_SCAN_PREVIEW_INTERVAL_SECONDS = 1.5
_EVENTS_PER_TICK = 64
_EVENT_POLL_MS = 120

# (path, size in bytes, is a whole directory)
Row = tuple[str, int, bool]
# (largest rows rendered during scan, effective item count, effective bytes)
PartialBucket = tuple[tuple[Row, ...], int, int]

_HIDDEN = 0
_DELETE_BUCKET = 1
_UNSURE_BUCKET = 2
_NANOSECONDS_PER_DAY = 86_400_000_000_000


@dataclass(frozen=True, slots=True)
class _AiExportGroup:
    """One model question and the same-type files it represents."""

    members: tuple[TriageItem, ...]
    filename_pattern: str | None

    @property
    def representative(self) -> TriageItem:
        return self.members[0]

    @property
    def total_logical_size(self) -> int:
        return sum(item.logical_size for item in self.members)

    def similarity_summary(self) -> AiReviewSimilarityGroup | None:
        if len(self.members) < 2 or self.filename_pattern is None:
            return None
        timestamps = [
            item.record.last_write_time_ns
            for item in self.members
            if item.record.last_write_time_ns is not None
        ]
        sizes = [item.logical_size for item in self.members]
        return AiReviewSimilarityGroup(
            filename_pattern=self.filename_pattern,
            member_count=len(self.members),
            total_logical_size_bytes=sum(sizes),
            minimum_logical_size_bytes=min(sizes),
            maximum_logical_size_bytes=max(sizes),
            oldest_last_write_time_ns=min(timestamps) if timestamps else None,
            newest_last_write_time_ns=max(timestamps) if timestamps else None,
        )


def _ai_age_band(
    item: TriageItem,
    *,
    now_ns: int,
    thresholds: tuple[int, ...],
) -> int | None:
    modified = item.record.last_write_time_ns
    if modified is None:
        return None
    age_days = max(0, (now_ns - modified) // _NANOSECONDS_PER_DAY)
    return sum(age_days >= threshold for threshold in thresholds)


def _ai_size_band(size: int) -> int:
    """Keep tiny files together while separating models/archives from metadata."""

    for index, ceiling in enumerate(
        (64 * 1024, 1024 * 1024, 16 * 1024 * 1024, 256 * 1024 * 1024)
    ):
        if size <= ceiling:
            return index
    return 4


def _group_ai_candidates(
    items: Sequence[TriageItem],
    rules: UserRules,
    *,
    now: datetime | None = None,
) -> tuple[_AiExportGroup, ...]:
    """Merge only generated-name siblings with equivalent review evidence."""

    current = now or datetime.now(UTC)
    now_ns = int(current.timestamp() * 1_000_000_000)
    thresholds = tuple(
        sorted(
            {
                1,
                7,
                30,
                90,
                rules.delete.classification.old_temp_days,
                rules.delete.classification.stale_metadata_days,
            }
        )
    )
    grouped: dict[tuple[object, ...], list[TriageItem]] = {}
    patterns: dict[tuple[object, ...], str | None] = {}
    for index, item in enumerate(items):
        if item.target_kind is not CleanupTargetKind.FILE:
            raise ValueError("AI review grouping accepts files only")
        pattern = reusable_path_pattern(item.path)
        filename_pattern = os.path.basename(pattern) if pattern else None
        if filename_pattern is None or not any(
            token in filename_pattern for token in ("*", "?")
        ):
            key: tuple[object, ...] = ("single", index)
            patterns[key] = None
        else:
            key = (
                "generated-sibling",
                normalise_path(os.path.dirname(item.path)),
                filename_pattern.casefold(),
                item.category,
                item.source_domain,
                item.lane,
                item.risk_tier,
                item.evidence_kind,
                item.actionability,
                item.execution_policy,
                item.recovery,
                item.reason,
                item.tags,
                item.record.allocation_uncertain,
                item.record.hardlink_duplicate,
                _ai_age_band(item, now_ns=now_ns, thresholds=thresholds),
                _ai_size_band(item.logical_size),
            )
            patterns[key] = filename_pattern
        grouped.setdefault(key, []).append(item)
    groups = [
        _AiExportGroup(tuple(members), patterns[key])
        for key, members in grouped.items()
    ]
    return tuple(
        sorted(
            groups,
            key=lambda group: group.total_logical_size,
            reverse=True,
        )
    )


def _open_path_in_explorer(path: str) -> Path:
    """Open a directory or select a file, falling back to its nearest parent."""

    target = Path(path)
    if target.is_dir():
        os.startfile(target)
        return target
    if target.exists():
        subprocess.Popen(["explorer.exe", f"/select,{target}"])
        return target
    parent = target.parent
    while parent != parent.parent and not parent.is_dir():
        parent = parent.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"路径及其父目录都不存在：{path}")
    os.startfile(parent)
    return parent


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
) -> tuple[PartialBucket, PartialBucket]:
    """Build bounded live previews without repeatedly sorting the full scan."""

    kept_paths = session.configured_keep_paths(rules)
    bucketed = tuple(
        (item, _item_bucket(item, rules, kept_paths))
        for item in session.iter_items()
    )
    effective_deletable = {
        id(item)
        for item in _drop_targets_covered_by_directory(
            tuple(
                item
                for item, bucket in bucketed
                if bucket == _DELETE_BUCKET
            )
        )
    }
    heaps: dict[int, list[tuple[int, int, Row]]] = {
        _DELETE_BUCKET: [],
        _UNSURE_BUCKET: [],
    }
    counts = {_DELETE_BUCKET: 0, _UNSURE_BUCKET: 0}
    totals = {_DELETE_BUCKET: 0, _UNSURE_BUCKET: 0}
    sequence = 0
    for item, bucket in bucketed:
        if bucket == _HIDDEN:
            continue
        if bucket == _DELETE_BUCKET and id(item) not in effective_deletable:
            continue
        is_directory = item.target_kind is CleanupTargetKind.DIRECTORY
        size = (
            session.subtree_totals(item.path).logical_bytes
            if is_directory
            else item.logical_size
        )
        counts[bucket] += 1
        totals[bucket] += size
        row = (item.path, size, is_directory)
        heap = heaps[bucket]
        ranked = (size, sequence, row)
        sequence += 1
        if len(heap) < _LIVE_ROWS_DRAWN:
            heapq.heappush(heap, ranked)
        elif size > heap[0][0]:
            heapq.heapreplace(heap, ranked)

    def rendered(bucket: int) -> PartialBucket:
        rows = tuple(
            entry[2]
            for entry in sorted(heaps[bucket], key=lambda entry: entry[0], reverse=True)
        )
        return (rows, counts[bucket], totals[bucket])

    return (rendered(_DELETE_BUCKET), rendered(_UNSURE_BUCKET))


def _partition_items(
    session: TriageSession, rules: UserRules
) -> tuple[tuple[TriageItem, ...], tuple[TriageItem, ...]]:
    """Apply the one authoritative KEEP/DELETE/AI partition."""

    deletable: list[TriageItem] = []
    unsure: list[TriageItem] = []
    kept_paths = session.configured_keep_paths(rules)
    for item in session.iter_items():
        bucket = _item_bucket(item, rules, kept_paths)
        if bucket == _DELETE_BUCKET:
            deletable.append(item)
        elif bucket == _UNSURE_BUCKET:
            unsure.append(item)
    # A whole-directory row already represents every descendant. Showing its
    # children as independent checkboxes would make an unticked child look
    # excluded even though deleting the checked parent still removes it.
    return (_drop_targets_covered_by_directory(deletable), tuple(unsure))


def _item_bucket(
    item: TriageItem,
    rules: UserRules,
    kept_paths: tuple[str, ...],
) -> int:
    decision = rules.decision_for(item.path)
    if decision is RuleDecision.KEEP:
        return _HIDDEN
    if (
        item.target_kind is CleanupTargetKind.DIRECTORY
        and _directory_contains_kept_path(item.path, kept_paths)
    ):
        return _HIDDEN
    if is_direct_cleanup_eligible(item) or (
        decision is RuleDecision.DELETE and _configured_delete_eligible(item)
    ):
        return _DELETE_BUCKET
    if is_ai_review_eligible(item):
        return _UNSURE_BUCKET
    return _HIDDEN


def _directory_contains_kept_path(
    directory: str,
    kept_paths: tuple[str, ...],
) -> bool:
    normalized = normalise_path(directory)
    prefix = normalized.rstrip(os.sep) + os.sep
    return any(path == normalized or path.startswith(prefix) for path in kept_paths)


def _drop_targets_covered_by_directory(
    items: Sequence[TriageItem],
) -> tuple[TriageItem, ...]:
    """Return the effective actions represented by a checkbox selection."""

    directories = sorted(
        {
            normalise_path(item.path).rstrip(os.sep) + os.sep
            for item in items
            if item.target_kind is CleanupTargetKind.DIRECTORY
        },
        key=len,
    )
    outer_directories = [
        prefix
        for prefix in directories
        if not any(prefix.startswith(parent) for parent in directories if len(parent) < len(prefix))
    ]

    def is_effective(item: TriageItem) -> bool:
        normalized = normalise_path(item.path)
        if item.target_kind is CleanupTargetKind.DIRECTORY:
            return normalized.rstrip(os.sep) + os.sep in outer_directories
        return not any(normalized.startswith(prefix) for prefix in outer_directories)

    return tuple(item for item in items if is_effective(item))


def _verdicts_from_session_index(
    text: str,
) -> tuple[str, bool, dict[str, tuple[str, str]]]:
    """Strictly validate supplied rows while allowing partial restart imports."""

    try:
        payload = json.loads(text)
        session = payload["review_session_id"]
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise AiReviewContractError("AI response has no valid review_session_id") from error
    if not isinstance(session, str):
        raise AiReviewContractError("AI response review_session_id must be text")
    known = ai_sessions.recall_export(session)
    if known is None:
        raise AiReviewContractError("AI response session is not in the persisted index")
    recovered = parse_partial_ai_review_response(
        text,
        expected_session_id=session,
        expected_nonce=known.nonce,
        expected_package_digest=known.package_digest,
        candidate_paths=known.candidate_paths,
    )
    members_by_path = {
        known.candidate_paths[candidate_id]: members
        for candidate_id, members in known.candidate_members.items()
    }
    expanded: dict[str, tuple[str, str]] = {}
    for path, recommendation, reason in recovered:
        for member_path in members_by_path.get(path, (path,)):
            expanded[member_path] = (recommendation.value, reason)
    return (
        session,
        len(recovered) == len(known.candidate_paths),
        expanded,
    )


def _expanded_live_verdicts(
    imported: AiReviewImport,
    candidate_members: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, str, str], ...]:
    """Apply one validated grouped answer to every represented live path."""

    expanded: list[tuple[str, str, str]] = []
    for entry in imported.recommendations:
        verdict = {
            AiRecommendation.DELETE: AiRecommendation.DELETE.value,
            AiRecommendation.KEEP: AiRecommendation.KEEP.value,
        }.get(entry.recommendation, AiRecommendation.UNSURE.value)
        for member_path in candidate_members.get(
            entry.candidate_id, (entry.item.path,)
        ):
            expanded.append((member_path, verdict, entry.reason))
    return tuple(expanded)


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
        self._pending_event: tuple[str, Any] | None = None
        self._known_roots: tuple[KnownCleanupRoot, ...] = ()
        self._session: TriageSession | None = None
        self._cancel: CancellationToken | None = None
        self._scan_token = ""
        self._delete_token = ""
        self._scan_session_id = uuid4().hex

        self._deletable: list[TriageItem] = []
        self._unsure: list[TriageItem] = []
        # Paths whose checkbox is ticked.  Only these are ever deleted.
        self._checked: set[str] = set()
        # Every package exported this session, so an answer can be imported
        # whichever export it came from.
        self._ai_packages: dict[str, AiReviewPackage] = {}
        self._ai_group_members: dict[
            str, dict[str, tuple[str, ...]]
        ] = {}
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
            hint=(
                "工具已确定是缓存或可再生产物。默认全部勾选，点方框可单独取消；"
                "双击任意行可在资源管理器中打开。"
            ),
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
                "AI 判断可能不准确，使用外部或付费模型可能产生费用。"
                "导出文件包含本机完整路径，请自行选择可信的模型；"
                "同目录生成型文件名会合并提问；AI 仍不确定的由你决定。"
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
        tree.column(
            "path",
            width=1200,
            minwidth=400,
            anchor=tk.W,
            stretch=False,
        )
        tree.tag_configure("odd", background="#fafbfe")
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(
            holder, orient=tk.VERTICAL, command=tree.yview
        )
        horizontal = ttk.Scrollbar(
            holder, orient=tk.HORIZONTAL, command=tree.xview
        )
        tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        if checkable:
            # ttk has no checkbox cell, so the first column draws one and a click
            # inside that column toggles it.  Clicking anywhere else just selects
            # the row, which never deletes anything on its own.
            tree.bind("<Button-1>", self._on_row_click)
        tree.bind("<Double-1>", self._on_row_double_click)

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
        self._ai_group_members.clear()
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

        session = TriageSession(
            review_sample_per_category=(
                active_rules.scan.review_sample_per_category
            )
        )
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
        # use DevClean-data beside the EXE; source runs use the working folder.
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
        next_publish = time.monotonic() + _SCAN_PREVIEW_INTERVAL_SECONDS
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
                if record.kind in {
                    ScanRecordKind.FILE,
                    ScanRecordKind.DIRECTORY,
                }:
                    session.observe_path(record.path, active_rules)
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
                    # Building a preview walks every retained candidate.  Start
                    # the next interval only after that work finishes: setting
                    # the deadline beforehand made a preview slower than 1.5 s
                    # immediately overdue, so almost every subsequent file
                    # triggered another full walk.
                    buckets = _rows_of(session, active_rules)
                    next_publish = (
                        time.monotonic() + _SCAN_PREVIEW_INTERVAL_SECONDS
                    )
                    self._events.put(
                        ("scan_partial", (token, buckets))
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
                for item in items[:_ROWS_DRAWN]
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

    def _on_row_double_click(self, event: tk.Event) -> str | None:
        tree = cast(ttk.Treeview, event.widget)
        if tree.identify_region(event.x, event.y) != "cell":
            return None
        row = tree.identify_row(event.y)
        if not row:
            return None
        try:
            opened = _open_path_in_explorer(row)
        except OSError as error:
            messagebox.showerror("无法打开位置", str(error))
            return "break"
        self._status.set(f"已在资源管理器中打开：{opened}")
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
        effective = _drop_targets_covered_by_directory(self._deletable)
        found = sum(self._size_of(item) for item in effective)
        chosen = self._selected_items()
        shown = (
            f"，表中显示前 {_ROWS_DRAWN:,} 项"
            if len(self._deletable) > _ROWS_DRAWN
            else ""
        )
        if all(item.path in self._checked for item in self._deletable):
            self._deletable_total.set(
                f"{_format_bytes(found)}（{len(self._deletable):,} 行{shown}）"
            )
        else:
            checked_rows = sum(
                item.path in self._checked for item in self._deletable
            )
            self._deletable_total.set(
                f"{_format_bytes(sum(self._size_of(item) for item in chosen))}"
                f" / {_format_bytes(found)}"
                f"（已勾选 {checked_rows:,} / {len(self._deletable):,} 行）"
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
        return _drop_targets_covered_by_directory(ticked)

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
        self._delete_token = token
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
        for prepared, item in enumerate(items, start=1):
            try:
                candidates.append(self._candidate(item))
            except (CleanupRefusal, OSError, TypeError, ValueError) as error:
                # "N items could not be processed" answers nothing.  Group by
                # what actually went wrong so the number is explainable.
                reasons[_reason_of(error)] = reasons.get(_reason_of(error), 0) + 1
            self._events.put(
                (
                    "delete_prepare_progress",
                    (token, mode, prepared, len(items), len(candidates)),
                )
            )
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
        self._events.put(
            ("delete_execute_started", (token, mode, len(plan.actions)))
        )
        done = 0
        for batch in plan.batches:
            last_update = 0.0

            def report(
                progress: CleanupExecutionProgress,
                *,
                completed_before: int = done,
            ) -> None:
                nonlocal last_update
                now = time.monotonic()
                if not progress.completed and now - last_update < 0.1:
                    return
                last_update = now
                self._events.put(
                    (
                        "delete_action_progress",
                        (
                            token,
                            mode,
                            completed_before + progress.action_index,
                            len(plan.actions),
                            progress,
                        ),
                    )
                )

            try:
                results.append(
                    execute_cleanup_batch(
                        batch,
                        mode,
                        known_roots=self._known_roots,
                        delete_config=self._scan_rules.delete.classification,
                        keep_config=self._scan_rules.keep.classification,
                        on_progress=report,
                    )
                )
            except Exception as error:
                # One batch failing says nothing about the next.  Record why and
                # carry on; stopping here is what made a single changed cache
                # file end a 7,000-item run.
                reason = _reason_of(error)
                reasons[reason] = reasons.get(reason, 0) + len(batch.actions)
            done += len(batch.actions)
            self._events.put(
                ("delete_progress", (token, mode, done, len(plan.actions)))
            )
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
        groups = _group_ai_candidates(self._unsure, self._rules)
        # Everything is represented. Generated-name siblings with equivalent
        # evidence become one question; unrelated semantic filenames stay
        # separate. A model cannot read hundreds of KB in one pass, so the
        # questions are split into numbered volumes rather than truncated.
        volumes = [
            groups[offset : offset + _AI_VOLUME_ITEMS]
            for offset in range(0, len(groups), _AI_VOLUME_ITEMS)
        ]
        built: list[
            tuple[
                AiReviewPackage,
                str,
                dict[str, tuple[str, ...]],
            ]
        ] = []
        try:
            for chunk in volumes:
                package = build_ai_review_package(
                    tuple(
                        AiReviewCandidateInput(
                            item=group.representative,
                            hard_protected=False,
                            similar_group=group.similarity_summary(),
                        )
                        for group in chunk
                    ),
                    scan_session_id=self._scan_session_id,
                    # Product decision: a path-redacted cleanup question usually
                    # produces UNSURE and wastes the model cost. The UI warns
                    # that the export contains local paths before this action.
                    disclose_full_paths=True,
                )
                candidate_members = {
                    entry.candidate_id: tuple(
                        member.path for member in group.members
                    )
                    for entry, group in zip(package.entries, chunk, strict=True)
                }
                built.append(
                    (
                        package,
                        serialize_ai_review_package(package) + "\n",
                        candidate_members,
                    )
                )
        except (AiReviewContractError, TypeError, ValueError) as error:
            messagebox.showerror("导出失败", str(error))
            return

        chosen = filedialog.asksaveasfilename(
            title="导出给 AI 判断",
            defaultextension=".json",
            initialfile=(
                f"devclean-ai-{len(groups)}questions-"
                f"{len(self._unsure)}files.json"
            ),
            filetypes=(("JSON", "*.json"),),
        )
        if not chosen:
            return
        base = Path(chosen)
        written: list[Path] = []
        try:
            for index, (_package, rendered, _members) in enumerate(
                built, start=1
            ):
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

        for package, _rendered, candidate_members in built:
            self._ai_packages[package.review_session_id] = package
            self._ai_group_members[package.review_session_id] = (
                candidate_members
            )
            # Losing the index entry only costs the restart-proof import path.
            with contextlib.suppress(OSError):
                ai_sessions.remember_export(
                    package.review_session_id,
                    package.nonce,
                    package.package_digest,
                    {entry.candidate_id: entry.item.path for entry in package.entries},
                    candidate_members,
                )
        self._sync_buttons()
        merged = len(self._unsure) - len(groups)
        summary = (
            f"{len(self._unsure):,} 个文件合并为 {len(groups):,} 个 AI 判断"
            f"（减少 {merged:,} 个重复问题）"
        )
        if len(written) == 1:
            self._status.set(
                f"{summary}，已导出到 {written[0]}。"
                "AI 可能判断错误且可能产生费用；回答后点「导入结果」。"
            )
        else:
            self._status.set(
                f"{summary}，已分成 {len(written)} 卷导出到 "
                f"{base.parent}，文件名 {base.stem}-1of{len(written)} 起。"
                "AI 可能判断错误且可能产生费用；每卷分别回答并逐份导入。"
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
        imported = None
        imported_session = ""
        try:
            routing_payload = json.loads(text)
            response_session = routing_payload.get("review_session_id")
        except (AttributeError, TypeError, ValueError):
            response_session = None
        live_package = (
            self._ai_packages.get(response_session)
            if isinstance(response_session, str)
            else None
        )
        if live_package is not None:
            try:
                imported = parse_ai_review_response(text, live_package)
            except (AiReviewContractError, TypeError, ValueError) as error:
                # A package still held in memory must use the complete contract.
                # The partial path exists only because a restart discarded that
                # sealed package; it must never relax a same-run failed import.
                messagebox.showerror("导入失败", str(error))
                return
            imported_session = live_package.review_session_id
        rule_verdicts: list[tuple[str, RuleDecision, str]] = []
        deletable: set[str] = set()
        keep: set[str] = set()
        needs_user: set[str] = set()
        fallback_complete = False

        def record(path: str, verdict: str, reason: str) -> None:
            key = normalise_path(path)
            if verdict == AiRecommendation.DELETE.value:
                self._ai_unsure_reasons.pop(key, None)
                deletable.add(key)
                rule_verdicts.append((path, RuleDecision.DELETE, reason))
            elif verdict == AiRecommendation.KEEP.value:
                self._ai_unsure_reasons.pop(key, None)
                keep.add(key)
                rule_verdicts.append((path, RuleDecision.KEEP, reason))
            else:
                needs_user.add(key)
                self._ai_unsure_reasons[key] = reason

        if imported is not None:
            live_members = self._ai_group_members.get(
                imported.review_session_id, {}
            )
            for member_path, verdict, reason in _expanded_live_verdicts(
                imported, live_members
            ):
                record(member_path, verdict, reason)
        else:
            # The export this answers was made before a restart, so the sealed
            # package is gone.  The session index still knows which path each
            # candidate id meant, and that is enough: the strict partial parser
            # still validates every supplied row, and every target is
            # re-verified by identity at deletion.
            try:
                recovered_session, fallback_complete, recovered = (
                    _verdicts_from_session_index(text)
                )
            except (AiReviewContractError, TypeError, ValueError) as error:
                messagebox.showerror(
                    "导入失败",
                    str(error) or "这份结果不属于任何已导出的卷。",
                )
                return
            imported_session = recovered_session
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
        for key in tuple(deletable):
            if self._rules.decision_for(key) is RuleDecision.KEEP:
                deletable.remove(key)
                keep.add(key)
        if rules_saved and self._session is not None:
            # One classification function owns both queues.  Rebuilding from the
            # completed session applies imported decisions, pre-existing regex
            # rules, and KEEP precedence to rows that were already on the left.
            self._publish(self._session)
            deletable_paths = {
                normalise_path(item.path) for item in self._deletable
            }
            moved_count = len(deletable & deletable_paths)
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
                if normalise_path(item.path) in deletable
            ]
            settled = deletable | keep
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
        decided_paths = deletable | keep
        status += (
            f"已保存 {len(decided_paths):,} 项结论；同路径及可识别的同类动态路径"
            "以后不再询问。"
            if rules_saved
            else "本次结果已应用，但未写入规则；下次仍可能再次询问。"
        )
        if (
            rules_saved
            and imported_session
            and not needs_user
            and (imported is not None or fallback_complete)
        ):
            # Complete imports consume the index entry. Partial restart imports
            # remain available for later volumes by explicit product decision.
            with contextlib.suppress(OSError):
                ai_sessions.forget_export(imported_session)
            self._ai_packages.pop(imported_session, None)
            self._ai_group_members.pop(imported_session, None)
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
        processed = 0
        try:
            while processed < _EVENTS_PER_TICK:
                pending = getattr(self, "_pending_event", None)
                if pending is None:
                    kind, payload = self._events.get_nowait()
                else:
                    kind, payload = pending
                    self._pending_event = None
                processed += 1
                # Progress and live previews are snapshots, not state
                # transitions.  If newer snapshots are already queued, render
                # only the newest one instead of making Tk replay stale screens.
                if kind in {"progress", "scan_partial"}:
                    kind, payload, consumed = self._newest_scan_snapshot(
                        kind,
                        payload,
                        _EVENTS_PER_TICK - processed,
                    )
                    processed += consumed
                if kind == "progress":
                    token, files, logical = cast(tuple[str, int, int], payload)
                    if token == self._scan_token:
                        self._status.set(
                            f"正在扫描…已看过 {files:,} 个文件"
                            f"（{_format_bytes(logical)}）"
                        )
                elif kind == "scan_partial":
                    token, buckets = cast(
                        tuple[str, tuple[PartialBucket, PartialBucket]], payload
                    )
                    if token != self._scan_token:
                        continue
                    # Shown as it is found.  Ticking and deleting stay disabled
                    # until the scan finishes, because a candidate has to be
                    # built from the completed session.
                    partial_deletable, partial_unsure = buckets
                    deletable_rows, deletable_count, deletable_bytes = (
                        partial_deletable
                    )
                    unsure_rows, unsure_count, unsure_bytes = partial_unsure
                    self._fill_rows(self._deletable_tree, deletable_rows)
                    self._fill_rows(self._unsure_tree, unsure_rows)
                    self._deletable_total.set(
                        f"{_format_bytes(deletable_bytes)}"
                        f"（{deletable_count:,} 项，扫描中）"
                    )
                    self._unsure_total.set(
                        f"{_format_bytes(unsure_bytes)}"
                        f"（{unsure_count:,} 项，扫描中）"
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
                elif kind == "delete_prepare_progress":
                    token, mode, done, total, accepted = cast(
                        tuple[str, CleanupMode, int, int, int], payload
                    )
                    if token != self._delete_token:
                        continue
                    self._progress.stop()
                    self._progress.configure(
                        mode="determinate", maximum=total, value=done
                    )
                    label = (
                        "彻底删除"
                        if mode is CleanupMode.PERMANENT
                        else "删除（进回收站）"
                    )
                    self._status.set(
                        f"正在准备{label}… {done:,} / {total:,}"
                        f"（可处理 {accepted:,} 项）"
                    )
                elif kind == "delete_execute_started":
                    token, mode, total = cast(
                        tuple[str, CleanupMode, int], payload
                    )
                    if token != self._delete_token:
                        continue
                    if mode is CleanupMode.RECYCLE:
                        self._progress.configure(mode="indeterminate", value=0)
                        self._progress.start(60)
                        self._status.set(
                            f"Windows 正在移入回收站…共 {total:,} 项"
                        )
                    else:
                        self._progress.stop()
                        self._progress.configure(
                            mode="determinate", maximum=total, value=0
                        )
                        self._status.set(f"正在彻底删除…共 {total:,} 项")
                elif kind == "delete_action_progress":
                    token, mode, before, total, progress = cast(
                        tuple[
                            str,
                            CleanupMode,
                            int,
                            int,
                            CleanupExecutionProgress,
                        ],
                        payload,
                    )
                    if token != self._delete_token:
                        continue
                    current = min(total, before + 1)
                    if mode is CleanupMode.PERMANENT:
                        fraction = (
                            1.0
                            if progress.completed
                            else min(
                                0.99,
                                progress.files_processed
                                / max(1, progress.files_total),
                            )
                        )
                        self._progress.configure(
                            mode="determinate",
                            maximum=total,
                            value=min(total, before + fraction),
                        )
                        if (
                            progress.target_kind
                            is CleanupTargetKind.DIRECTORY
                            and not progress.completed
                        ):
                            self._status.set(
                                f"正在彻底删除第 {current:,} / {total:,} 项："
                                f"当前目录已处理 "
                                f"{progress.files_processed:,} / "
                                f"{progress.files_total:,} 个文件"
                            )
                        else:
                            self._status.set(
                                f"正在彻底删除… {min(total, before + int(progress.completed)):,}"
                                f" / {total:,}"
                            )
                    else:
                        # Windows owns a Recycle Bin directory operation and
                        # exposes no reliable per-child counter. Keep the bar
                        # moving and report the exact item ordinal.
                        self._status.set(
                            f"正在移入回收站第 {current:,} / {total:,} 项"
                        )
                elif kind == "delete_progress":
                    token, mode, done, total = cast(
                        tuple[str, CleanupMode, int, int], payload
                    )
                    if token != self._delete_token:
                        continue
                    if mode is CleanupMode.PERMANENT:
                        self._progress.configure(
                            mode="determinate", maximum=total, value=done
                        )
                    self._status.set(
                        f"正在{'彻底删除' if mode is CleanupMode.PERMANENT else '移入回收站'}"
                        f"… {done:,} / {total:,}"
                    )
                elif kind == "delete_done":
                    token, results, reasons = cast(
                        tuple[str, tuple[CleanupExecutionResult, ...], dict[str, int]],
                        payload,
                    )
                    if token != self._delete_token:
                        continue
                    self._progress.stop()
                    self._progress.configure(mode="indeterminate", value=0)
                    self._busy = None
                    self._report_deletion(results, reasons)
                elif kind == "delete_error":
                    token, detail, results = cast(
                        tuple[str, str, tuple[CleanupExecutionResult, ...]], payload
                    )
                    if token != self._delete_token:
                        continue
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
        finally:
            self._root.after(
                1 if processed >= _EVENTS_PER_TICK else _EVENT_POLL_MS,
                self._drain_events,
            )

    def _newest_scan_snapshot(
        self,
        kind: str,
        payload: Any,
        limit: int,
    ) -> tuple[str, Any, int]:
        """Coalesce adjacent scan snapshots without crossing a real event.

        ``scan_done`` and deletion events must stay ordered.  Queue has no
        push-front, so the first different event is kept in one private slot and
        becomes the next event processed.
        """

        newest = payload
        consumed = 0
        while consumed < limit:
            try:
                queued_kind, queued_payload = self._events.get_nowait()
            except Empty:
                break
            if queued_kind == kind:
                newest = queued_payload
                consumed += 1
                continue
            self._pending_event = (queued_kind, queued_payload)
            break
        return kind, newest, consumed

    def _report_deletion(
        self,
        results: tuple[CleanupExecutionResult, ...],
        reasons: dict[str, int],
    ) -> None:
        finished = 0
        failed = 0
        freed = 0
        unverified_recycles = 0
        for result in results:
            for _action, state in result.action_states:
                if state in {ActionState.PURGED, ActionState.RECYCLED}:
                    finished += 1
                else:
                    failed += 1
            freed += result.purged_logical_bytes
            unverified_recycles += len(result.unverified_recycle_paths)
        failed += sum(reasons.values())
        gone = {
            path
            for result in results
            for path in result.completed_paths
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
        if unverified_recycles:
            parts.append(
                f"{unverified_recycles:,} 项已由 Windows 移除，但无法确认是否进入"
                "回收站，可能无法恢复"
            )
        for name, count in sorted(reasons.items(), key=lambda pair: -pair[1]):
            parts.append(f"{name} {count:,} 项")
        self._status.set("；".join(parts) + "。点「扫描」可核对结果。")


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
