"""Read-only result classification for the scan -> review -> cleanup workflow.

Classification describes evidence and review priority.  It never selects an
item, never grants execution authority, and never invokes an AI or cleanup
operation.  This separation is a permanent safety invariant.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    SourceDomain,
    known_root_for_path,
    source_domain_for_category,
)
from devclean.core.scan_insights import ScanInsights
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

_DISPLAY_LIMIT = 500
_OLD_TEMP_AGE = timedelta(days=7)
_LARGE_FILE_BYTES = 1 << 30
_STALE_METADATA_AGE = timedelta(days=90)
_PRIVATE_QUARANTINE_PREFIX = ".DevClean-quarantine-v1-"
_PROTECTED_SEGMENTS = frozenset(
    {
        ".git",
        ".codex",
        ".claude",
        "globalstorage",
        "local history",
    }
)
_DEVELOPMENT_CACHE_SEGMENTS = frozenset(
    {
        "huggingface",
        "pip",
        "uv",
        "npm-cache",
        "pnpm-store",
        "yarn",
        "gradle",
    }
)
_PROTECTED_SUFFIXES = frozenset({".key", ".pem", ".pfx", ".p12", ".kdbx"})
_BUILD_SEGMENTS = frozenset(
    {
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        "target",
        "dist",
        "build",
        "out",
    }
)
_IDE_SEGMENTS = frozenset(
    {"code", "cursor", "windsurf", "jetbrains", "pycharm", "intellijidea"}
)
_CACHE_SEGMENTS = frozenset({"cache", "cacheddata", "gpucache", "caches"})
_CONTAINER_SEGMENTS = frozenset({"docker", "podman", "containers", "wsl", "wsl2"})


class ReviewLane(StrEnum):
    """Human review queues; none implies automatic execution."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    VENDOR_MANAGED = "VENDOR_MANAGED"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
    PROTECTED = "PROTECTED"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    PROTECTED = "PROTECTED"


class EvidenceKind(StrEnum):
    AGE_AND_APPROVED_ROOT = "AGE_AND_APPROVED_ROOT"
    KNOWN_ROOT_HEURISTIC = "KNOWN_ROOT_HEURISTIC"
    PATH_HEURISTIC = "PATH_HEURISTIC"
    FILESYSTEM_OBSERVATION = "FILESYSTEM_OBSERVATION"
    PROTECTED_RULE = "PROTECTED_RULE"


class Actionability(StrEnum):
    """Which post-scan workflow may consider one observation."""

    REVIEW_PLAN = "REVIEW_PLAN"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"
    PROTECTED = "PROTECTED"


class ExecutionPolicy(StrEnum):
    """Locally assigned execution ceiling, independent from analysis lane."""

    PERMANENT_APPROVED_CACHE = "PERMANENT_APPROVED_CACHE"
    EXACT_VENDOR = "EXACT_VENDOR"
    PREVIEWED_VENDOR = "PREVIEWED_VENDOR"
    POLICY_VENDOR = "POLICY_VENDOR"
    RECYCLE_ONLY = "RECYCLE_ONLY"
    NONE = "NONE"


class RecoveryCapability(StrEnum):
    """Honest recovery claim before any future action exists."""

    UNKNOWN = "UNKNOWN"
    VENDOR_REDOWNLOAD_BEST_EFFORT = "VENDOR_REDOWNLOAD_BEST_EFFORT"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class TriageItem:
    record: ScanRecord
    path: str
    logical_size: int
    allocated_size: int | None
    category: CleanupCategory
    source_domain: SourceDomain
    lane: ReviewLane
    risk_tier: RiskTier
    evidence_kind: EvidenceKind
    actionability: Actionability
    execution_policy: ExecutionPolicy
    recovery: RecoveryCapability
    reason: str
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class TriageSummary:
    files: int = 0
    logical_bytes: int = 0
    allocated_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _Classification:
    category: CleanupCategory
    lane: ReviewLane
    risk_tier: RiskTier
    evidence_kind: EvidenceKind
    actionability: Actionability
    execution_policy: ExecutionPolicy
    recovery: RecoveryCapability
    reason: str
    tags: tuple[str, ...] = ()


class TriageSession:
    """Keep exact aggregates plus a bounded largest-item review sample."""

    def __init__(self, *, display_limit: int = _DISPLAY_LIMIT) -> None:
        if display_limit < 1:
            raise ValueError("display_limit must be positive")
        self._display_limit = display_limit
        self._summaries = {lane: TriageSummary() for lane in ReviewLane}
        self._items: dict[ReviewLane, list[TriageItem]] = {lane: [] for lane in ReviewLane}
        self._insights = ScanInsights()

    def add(self, item: TriageItem) -> None:
        summary = self._summaries[item.lane]
        summary.files += 1
        summary.logical_bytes += item.logical_size
        if item.allocated_size is not None:
            summary.allocated_bytes += item.allocated_size
        self._insights.add(item)

        displayed = self._items[item.lane]
        if len(displayed) < self._display_limit:
            displayed.append(item)
            return
        smallest = min(displayed, key=lambda existing: existing.logical_size)
        if item.logical_size > smallest.logical_size:
            displayed.remove(smallest)
            displayed.append(item)

    def summary(self, lane: ReviewLane) -> TriageSummary:
        source = self._summaries[lane]
        return TriageSummary(source.files, source.logical_bytes, source.allocated_bytes)

    def items(self, lane: ReviewLane) -> tuple[TriageItem, ...]:
        return tuple(sorted(self._items[lane], key=lambda item: item.logical_size, reverse=True))

    def all_display_items(self) -> tuple[TriageItem, ...]:
        return tuple(item for lane in ReviewLane for item in self.items(lane))

    @property
    def display_limit(self) -> int:
        return self._display_limit

    @property
    def insights(self) -> ScanInsights:
        return self._insights


def triage_file(
    record: ScanRecord,
    *,
    now: datetime | None = None,
    temp_root: Path | None = None,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> TriageItem:
    """Classify one file observation without reading contents or mutating state."""

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("triage accepts file observations only")
    classification = _classify(
        Path(record.path),
        record.last_write_time_ns,
        now=now,
        temp_root=temp_root,
        known_roots=known_roots,
    )
    tags = list(classification.tags)
    if record.logical_size >= _LARGE_FILE_BYTES:
        tags.append("large_file")
    if record.logical_size == 0:
        tags.append("empty_file")
    if _is_older_than(record.last_write_time_ns, _STALE_METADATA_AGE, now):
        tags.append("not_modified_90_days")
    if record.hardlink_duplicate:
        tags.append("hardlink_duplicate")
    if record.allocation_uncertain:
        tags.append("allocation_estimate")
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=record.logical_size,
        allocated_size=record.allocated_size,
        category=classification.category,
        source_domain=source_domain_for_category(classification.category),
        lane=classification.lane,
        risk_tier=classification.risk_tier,
        evidence_kind=classification.evidence_kind,
        actionability=classification.actionability,
        execution_policy=classification.execution_policy,
        recovery=classification.recovery,
        reason=classification.reason,
        tags=tuple(dict.fromkeys(tags)),
    )


def _classify(
    path: Path,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    temp_root: Path | None,
    known_roots: tuple[KnownCleanupRoot, ...],
) -> _Classification:
    if is_protected_path(path):
        return _Classification(
            CleanupCategory.OTHER,
            ReviewLane.PROTECTED,
            RiskTier.PROTECTED,
            EvidenceKind.PROTECTED_RULE,
            Actionability.PROTECTED,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "受保护的开发仓库、凭据或编辑器历史资产",
            ("protected",),
        )

    known = known_root_for_path(path, known_roots)
    if known is not None:
        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:
            if _is_older_than(last_write_time_ns, _OLD_TEMP_AGE, now):
                return _Classification(
                    known.category,
                    ReviewLane.DETERMINISTIC_CANDIDATE,
                    RiskTier.LOW,
                    EvidenceKind.AGE_AND_APPROVED_ROOT,
                    Actionability.REVIEW_PLAN,
                    ExecutionPolicy.PERMANENT_APPROVED_CACHE,
                    RecoveryCapability.UNKNOWN,
                    f"{known.label}：已知根目录且超过 7 天；仅作为人工计划候选",
                    ("known_root", "older_than_7_days"),
                )
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.RECYCLE_ONLY,
                RecoveryCapability.UNKNOWN,
                f"{known.label}：未达到 7 天阈值；可人工选择或先做 AI 复核",
                ("known_root", "recent", "ai_review_optional"),
            )
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.RECYCLE_ONLY,
                RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
                (
                    f"{known.label}：属于厂商管理存储；可人工选择或先做 AI 复核，"
                    "永久清除需独立强确认"
                ),
                ("known_root", "vendor_managed", "ai_review_optional"),
            )
        if known.policy is CleanupPolicy.REPORT_ONLY:
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.NONE,
                f"{known.label}：系统或厂商维护范围，只生成报告",
                ("known_root", "system_managed", "report_only"),
            )
        if known.policy is CleanupPolicy.MANUAL_REVIEW:
            # Browser/editor/application caches are plausible cleanup
            # candidates, but the owning application may still be running.
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.RECYCLE_ONLY,
                RecoveryCapability.UNKNOWN,
                f"{known.label}：已知缓存位置，但需确认应用状态与文件用途",
                ("known_root", "manual_review", "ai_review_optional"),
            )

    root = temp_root or Path(tempfile.gettempdir())
    if _is_descendant(path, root) and _is_older_than(last_write_time_ns, _OLD_TEMP_AGE, now):
        return _Classification(
            CleanupCategory.USER_TEMP,
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.AGE_AND_APPROVED_ROOT,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.PERMANENT_APPROVED_CACHE,
            RecoveryCapability.UNKNOWN,
            "当前用户临时目录中超过 7 天；仅作为人工计划候选",
            ("older_than_7_days",),
        )

    if is_development_cache_hint(path):
        category = _infer_presentation_category(path)
        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.RECYCLE_ONLY,
            RecoveryCapability.UNKNOWN,
            "路径看起来像开发缓存，但缺少厂商精确证据；需 AI 与人工复核",
            ("path_heuristic", "ai_review_required"),
        )

    category = _infer_presentation_category(path)
    if category in {
        CleanupCategory.PROJECT_BUILD_OUTPUT,
        CleanupCategory.INSTALLERS_DOWNLOADS,
        CleanupCategory.SYSTEM_LOGS,
    }:
        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.RECYCLE_ONLY,
            RecoveryCapability.UNKNOWN,
            "疑似构建产物、安装介质或日志；需 AI 建议与用户最终确认",
            ("path_heuristic", "ai_review_required"),
        )
    if category is CleanupCategory.WINDOWS_UPDATE:
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.FILESYSTEM_OBSERVATION,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "Windows 更新或组件存储只能交给 Windows 官方维护流程",
            ("system_managed",),
        )
    return _Classification(
        category,
        ReviewLane.AI_REVIEW,
        RiskTier.HIGH,
        EvidenceKind.FILESYSTEM_OBSERVATION,
        Actionability.AI_REVIEW,
        ExecutionPolicy.RECYCLE_ONLY,
        RecoveryCapability.UNKNOWN,
        "用途尚未确定；可导出受限元数据给 AI 分析，导回后仍需用户确认",
        ("ai_review_required",),
    )


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root)))
        ) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def is_protected_path(path: Path) -> bool:
    """Return whether a hard protection rule forbids every future action."""

    parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return bool(
        parts & _PROTECTED_SEGMENTS
        or any(part.startswith(_PRIVATE_QUARANTINE_PREFIX) for part in parts)
        or name == ".env"
        or name.startswith(".env.")
        or path.suffix.casefold() in _PROTECTED_SUFFIXES
    )


def is_development_cache_hint(path: Path) -> bool:
    """Return a presentation-only path hint; never an action decision."""

    return not is_protected_path(path) and bool(
        {part.casefold() for part in path.parts} & _DEVELOPMENT_CACHE_SEGMENTS
    )


def _infer_presentation_category(path: Path) -> CleanupCategory:
    """Infer a display category without changing risk or actionability."""

    parts = {part.casefold() for part in path.parts}
    suffix = path.suffix.casefold()
    if "windows.old" in parts or {"softwaredistribution", "download"}.issubset(parts):
        return CleanupCategory.WINDOWS_UPDATE
    if "winsxs" in parts:
        return CleanupCategory.WINDOWS_UPDATE
    if parts & _CONTAINER_SEGMENTS or suffix in {".vhd", ".vhdx"}:
        return CleanupCategory.CONTAINER_STORAGE
    if ".conda" in parts or "conda-meta" in parts:
        return CleanupCategory.CONDA_CACHE
    if parts & _IDE_SEGMENTS and parts & _CACHE_SEGMENTS:
        return CleanupCategory.IDE_CACHE
    if parts & _BUILD_SEGMENTS:
        return CleanupCategory.PROJECT_BUILD_OUTPUT
    if suffix in {".dmp", ".log", ".etl", ".evtx", ".tmp"}:
        return CleanupCategory.SYSTEM_LOGS
    # A generic .exe can be an application binary, developer tool, or user
    # asset.  Treat it as an installer only inside Downloads; package formats
    # whose purpose is explicit may be grouped without changing actionability.
    if "downloads" in parts or suffix in {
        ".msi",
        ".msix",
        ".msixbundle",
        ".appx",
        ".appxbundle",
        ".iso",
    }:
        return CleanupCategory.INSTALLERS_DOWNLOADS
    return CleanupCategory.OTHER


def _is_older_than(
    last_write_time_ns: int | None, age: timedelta, now: datetime | None
) -> bool:
    if last_write_time_ns is None:
        return False
    observed = datetime.fromtimestamp(last_write_time_ns / 1_000_000_000, tz=UTC)
    return observed <= (now or datetime.now(UTC)) - age


__all__ = [
    "Actionability",
    "CleanupCategory",
    "EvidenceKind",
    "ExecutionPolicy",
    "RecoveryCapability",
    "ReviewLane",
    "RiskTier",
    "TriageItem",
    "TriageSession",
    "TriageSummary",
    "is_development_cache_hint",
    "is_protected_path",
    "triage_file",
]
