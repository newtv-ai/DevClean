"""Read-only result classification for the scan -> review -> cleanup workflow.

Classification describes evidence and review priority. It never selects an
item, never grants execution authority, and never invokes an AI or cleanup
operation. This separation is a permanent safety invariant.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from heapq import heappush, heapreplace
from pathlib import Path

from devclean.core.application_cleanup import PolicyAction, evaluate_application_path
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    SourceDomain,
    known_root_for_path,
    source_domain_for_category,
)
from devclean.core.user_rules import (
    DeleteClassification,
    KeepClassification,
    RuleDecision,
    UserRules,
    normalise_path,
)
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind


class CleanupTargetKind(StrEnum):
    """Whether one observation stands for a single file or a whole subtree."""

    FILE = "FILE"
    DIRECTORY = "DIRECTORY"


class DirectoryScope(StrEnum):
    """Why a whole directory may be removed as one object, if it may at all."""

    KNOWN_CACHE_ROOT = "KNOWN_CACHE_ROOT"
    REGENERABLE_TOOL_OUTPUT = "REGENERABLE_TOOL_OUTPUT"
    STALE_VERSION = "STALE_VERSION"
    AGED_TEMP_ITEM = "AGED_TEMP_ITEM"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class ReviewLane(StrEnum):
    """Human review queues; none implies automatic execution."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"


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


class Actionability(StrEnum):
    """Which post-scan workflow may consider one observation."""

    REVIEW_PLAN = "REVIEW_PLAN"
    AI_REVIEW = "AI_REVIEW"
    REPORT_ONLY = "REPORT_ONLY"


class ExecutionPolicy(StrEnum):
    """Locally assigned execution ceiling, independent from analysis lane."""

    USER_CHOICE_DELETE = "USER_CHOICE_DELETE"
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
    target_kind: CleanupTargetKind = CleanupTargetKind.FILE
    directory_scope: DirectoryScope | None = None


@dataclass(frozen=True, slots=True)
class DirectorySubtreeTotals:
    """Exact totals accumulated for one whole-tree candidate during a scan."""

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
    """Keep the largest configured review sample in each lane/category."""

    def __init__(self, *, review_sample_per_category: int) -> None:
        if review_sample_per_category < 1:
            raise ValueError("review_sample_per_category must be positive")
        self._review_sample_per_category = review_sample_per_category
        self._items: dict[
            tuple[ReviewLane, CleanupCategory], list[tuple[int, int, TriageItem]]
        ] = {}
        self._sequence = 0
        self._observed_keep_paths: set[str] = set()
        self._observation_rules: UserRules | None = None
        self._keep_cache_rules: UserRules | None = None
        self._keep_cache: tuple[str, ...] = ()
        self._directory_totals: dict[str, DirectorySubtreeTotals] = {}
        self._ancestor_cache: dict[str, tuple[str, ...]] = {}

    def observe_path(self, path: str, rules: UserRules) -> None:
        """Retain only observed paths protected by a configured KEEP rule."""

        if self._observation_rules is None:
            self._observation_rules = rules
        elif self._observation_rules is not rules:
            raise ValueError("one scan session must use one pinned rule set")
        if rules.decision_for(path) is RuleDecision.KEEP:
            self._observed_keep_paths.add(normalise_path(path))
            self._keep_cache_rules = None
            self._keep_cache = ()

    def add(self, item: TriageItem) -> None:
        if item.target_kind is CleanupTargetKind.DIRECTORY:
            self._register_directory(item.path)
        else:
            self._accumulate_into_directories(item)

        if (
            item.lane is not ReviewLane.REPORT_ONLY
            and item.execution_policy is ExecutionPolicy.USER_CHOICE_DELETE
        ):
            bucket = self._items.setdefault((item.lane, item.category), [])
            self._sequence += 1
            entry = (item.logical_size, self._sequence, item)
            if len(bucket) < self._review_sample_per_category:
                heappush(bucket, entry)
            elif item.logical_size > bucket[0][0]:
                heapreplace(bucket, entry)

    def _register_directory(self, path: str) -> None:
        key = os.path.normcase(os.path.normpath(path))
        if key in self._directory_totals:
            return
        self._directory_totals[key] = DirectorySubtreeTotals()
        stale = [
            directory
            for directory in self._ancestor_cache
            if directory == key or directory.startswith(key + os.sep)
        ]
        for directory in stale:
            del self._ancestor_cache[directory]

    def _accumulate_into_directories(self, item: TriageItem) -> None:
        if not self._directory_totals:
            return
        parent = os.path.normcase(os.path.normpath(os.path.dirname(item.path)))
        ancestors = self._ancestor_cache.get(parent)
        if ancestors is None:
            ancestors = self._registered_ancestors(parent)
            self._ancestor_cache[parent] = ancestors
        if not ancestors:
            return
        allocated = item.allocated_size or 0
        for key in ancestors:
            current = self._directory_totals[key]
            self._directory_totals[key] = DirectorySubtreeTotals(
                files=current.files + 1,
                logical_bytes=current.logical_bytes + item.logical_size,
                allocated_bytes=current.allocated_bytes + allocated,
            )

    def _registered_ancestors(self, directory: str) -> tuple[str, ...]:
        """Return every registered candidate that contains *directory*."""

        found: list[str] = []
        current = directory
        while True:
            if current in self._directory_totals:
                found.append(current)
            parent = os.path.dirname(current)
            if parent == current:
                return tuple(found)
            current = parent

    def subtree_totals(self, path: str) -> DirectorySubtreeTotals:
        """Return the exact totals observed beneath one whole-tree candidate."""

        key = os.path.normcase(os.path.normpath(path))
        return self._directory_totals.get(key, DirectorySubtreeTotals())

    def iter_items(self) -> Iterator[TriageItem]:
        """Iterate the bounded review sample without copying or sorting it."""

        return (
            entry[2]
            for bucket in self._items.values()
            for entry in bucket
        )

    def configured_keep_paths(self, rules: UserRules) -> tuple[str, ...]:
        """Return sorted observed paths protected by the active KEEP rules."""

        if self._keep_cache_rules is rules:
            return self._keep_cache
        kept_paths = {
            path
            for path in self._observed_keep_paths
            if rules.decision_for(path) is RuleDecision.KEEP
        }
        kept_paths.update(
            normalise_path(item.path)
            for item in self.iter_items()
            if rules.decision_for(item.path) is RuleDecision.KEEP
        )
        kept = tuple(sorted(kept_paths))
        self._keep_cache_rules = rules
        self._keep_cache = kept
        return kept


def triage_file(
    record: ScanRecord,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    now: datetime | None = None,
    temp_root: Path | None = None,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> TriageItem:
    """Classify one file observation without reading contents or mutating state."""

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("triage accepts file observations only")
    classification = _classify(
        Path(record.path),
        record.logical_size,
        record.last_write_time_ns,
        now=now,
        temp_root=temp_root,
        known_roots=known_roots,
        delete_config=delete_config,
        keep_config=keep_config,
    )
    tags = list(classification.tags)
    if record.logical_size >= delete_config.large_file_bytes:
        tags.append("large_file")
    if record.logical_size == 0:
        tags.append("empty_file")
    if _is_older_than(
        record.last_write_time_ns,
        timedelta(days=delete_config.stale_metadata_days),
        now,
    ):
        tags.append("stale_metadata")
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
        source_domain=source_domain_for_category(
            classification.category, delete_config.category_source_domains
        ),
        lane=classification.lane,
        risk_tier=classification.risk_tier,
        evidence_kind=classification.evidence_kind,
        actionability=classification.actionability,
        execution_policy=classification.execution_policy,
        recovery=classification.recovery,
        reason=classification.reason,
        tags=tuple(dict.fromkeys(tags)),
    )


def triage_directory(
    record: ScanRecord,
    *,
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
    known_roots: tuple[KnownCleanupRoot, ...] = (),
) -> TriageItem | None:
    """Classify one directory observation as a whole-tree candidate, or skip it."""

    if record.kind is not ScanRecordKind.DIRECTORY:
        raise ValueError("directory triage accepts directory observations only")
    path = Path(record.path)
    if _normalized_path(path) == _normalized_path(Path(record.root)):
        known_scan_root = known_root_for_path(path, known_roots)
        if (
            known_scan_root is None
            or _normalized_path(known_scan_root.path) != _normalized_path(path)
            or not known_scan_root.delete_root_itself
        ):
            return None
    scope = directory_cleanup_scope(path, known_roots, delete_config, keep_config)
    if scope is DirectoryScope.NOT_ELIGIBLE:
        return None
    known = known_root_for_path(path, known_roots)
    if scope is DirectoryScope.KNOWN_CACHE_ROOT and known is not None:
        category = known.category
        recovery = (
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
            if known.policy is CleanupPolicy.VENDOR_MANAGED
            else RecoveryCapability.UNKNOWN
        )
        reason = f"{known.label}：整个目录属于已识别的厂商存储，可作为单个对象清理"
        tags = ("whole_directory", "known_cache_root")
    elif scope is DirectoryScope.AGED_TEMP_ITEM:
        category = CleanupCategory.USER_TEMP
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        reason = (
            f"{path.name}：临时目录中超过 "
            f"{delete_config.old_temp_days} 天未改动的整个条目"
        )
        tags = ("whole_directory", "aged_temp_item")
    elif scope is DirectoryScope.STALE_VERSION:
        category = CleanupCategory.OTHER
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        reason = f"{path.name}：已被更新取代的旧版本目录，同级已有更新的版本"
        tags = ("whole_directory", "stale_version")
    else:
        category = CleanupCategory.PROJECT_BUILD_OUTPUT
        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
        reason = f"{path.name}：工具确定性重建的产物目录，可作为单个对象清理"
        tags = ("whole_directory", "regenerable_tool_output")
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=0,
        allocated_size=None,
        category=category,
        source_domain=source_domain_for_category(
            category, delete_config.category_source_domains
        ),
        lane=ReviewLane.AI_REVIEW,
        risk_tier=RiskTier.HIGH,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.AI_REVIEW,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=recovery,
        reason=reason,
        tags=tags,
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=scope,
    )


def _classify(
    path: Path,
    logical_size: int,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    temp_root: Path | None,
    known_roots: tuple[KnownCleanupRoot, ...],
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
) -> _Classification:
    application = _application_classification(
        path,
        logical_size,
        last_write_time_ns,
        now=now,
        delete_config=delete_config,
    )
    if application is not None:
        return application

    if is_regenerable_byproduct(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            "日志、转储或临时产物；产生它的程序会重新写出来",
            ("byproduct",),
        )

    if is_inside_cache_directory(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            "位于名为 cache 的目录内；缓存由产生它的程序自行重建",
            ("cache_directory",),
        )

    if is_installed_addon_payload(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "已安装的扩展或捆绑运行时的一部分，不是垃圾；只能靠重装该扩展恢复",
            ("installed_payload",),
        )
    known = known_root_for_path(path, known_roots)
    if known is not None:
        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:
            if _is_older_than(
                last_write_time_ns,
                timedelta(days=delete_config.old_temp_days),
                now,
            ):
                return _Classification(
                    known.category,
                    ReviewLane.DETERMINISTIC_CANDIDATE,
                    RiskTier.LOW,
                    EvidenceKind.AGE_AND_APPROVED_ROOT,
                    Actionability.REVIEW_PLAN,
                    ExecutionPolicy.USER_CHOICE_DELETE,
                    RecoveryCapability.UNKNOWN,
                    (
                        f"{known.label}：已知根目录且超过 "
                        f"{delete_config.old_temp_days} 天，判定可以删除；执行仍需你确认"
                    ),
                    ("known_root", "older_than_configured_days"),
                )
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                (
                    f"{known.label}：属于配置列明的已知临时目录，但未达到 "
                    f"{delete_config.old_temp_days} 天阈值；左栏默认勾选，可自行取消"
                ),
                ("known_root", "recent"),
            )
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
                (
                    f"{known.label}：属于配置列明的厂商管理存储；工具判定可清理，"
                    "实际方式仍由你在左栏选择"
                ),
                ("known_root", "vendor_managed"),
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
            return _Classification(
                known.category,
                ReviewLane.AI_REVIEW,
                RiskTier.HIGH,
                EvidenceKind.KNOWN_ROOT_HEURISTIC,
                Actionability.AI_REVIEW,
                ExecutionPolicy.USER_CHOICE_DELETE,
                RecoveryCapability.UNKNOWN,
                f"{known.label}：配置列明的已知缓存位置；清理前可自行关闭相关应用",
                ("known_root", "manual_review"),
            )

    root = temp_root or Path(tempfile.gettempdir())
    if _is_descendant(path, root) and _is_older_than(
        last_write_time_ns,
        timedelta(days=delete_config.old_temp_days),
        now,
    ):
        return _Classification(
            CleanupCategory.USER_TEMP,
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.AGE_AND_APPROVED_ROOT,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.UNKNOWN,
            (
                f"当前用户临时目录中超过 {delete_config.old_temp_days} 天，"
                "判定可以删除；执行仍需你确认"
            ),
            ("older_than_configured_days",),
        )

    if is_program_payload_file(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "程序本体、其载入的库，或虚拟磁盘；不是垃圾，释放这类空间要用它自己的工具",
            ("program_payload",),
        )
    if is_application_state_file(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "程序的配置或运行状态，不是垃圾；删除会改变程序行为",
            ("application_state",),
        )

    if is_development_cache_hint(path, delete_config):
        category = _infer_presentation_category(path, delete_config)
        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.UNKNOWN,
            "路径看起来像开发缓存，但缺少厂商精确证据；不确定，交 AI 判断",
            ("path_heuristic", "ai_review_required"),
        )

    category = _infer_presentation_category(path, delete_config)
    if category.value in delete_config.inferred_ai_review_categories:
        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.UNKNOWN,
            "疑似构建产物、安装介质或日志；需 AI 建议与用户最终确认",
            ("path_heuristic", "ai_review_required"),
        )
    if category.value in delete_config.inferred_report_only_categories:
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
        ExecutionPolicy.USER_CHOICE_DELETE,
        RecoveryCapability.UNKNOWN,
        "本工具无法确定这是什么；导出给 AI 判断，导回结果后由你确认执行",
        ("ai_review_required",),
    )


def _application_classification(
    path: Path,
    logical_size: int,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    delete_config: DeleteClassification,
) -> _Classification | None:
    last_used = None
    if last_write_time_ns is not None:
        last_used = datetime.fromtimestamp(last_write_time_ns / 1_000_000_000, tz=UTC)
    decision = evaluate_application_path(
        path,
        logical_size=logical_size,
        last_used=last_used,
        now=now,
    )
    if decision is None:
        return None

    rule = decision.rule
    category = _infer_presentation_category(path, delete_config)
    if "crashpad" in rule.rule_id:
        category = CleanupCategory.CRASH_DUMPS
    elif "log" in rule.rule_id:
        category = CleanupCategory.SYSTEM_LOGS
    elif any(token in rule.rule_id for token in ("cache", "plugin")):
        category = CleanupCategory.IDE_CACHE

    common_tags = (
        "application_policy",
        f"application:{rule.app_id}",
        f"rule:{rule.rule_id}",
    )
    if decision.action is PolicyAction.KEEP_PROTECTED:
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.KNOWN_ROOT_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            f"{rule.label}：属于应用持久状态，不按缓存或文件后缀清理",
            (*common_tags, "application_keep"),
        )

    if decision.action is PolicyAction.USER_DECISION:
        bucket = decision.age_bucket or "unknown-age"
        return _Classification(
            category,
            ReviewLane.AI_REVIEW,
            RiskTier.HIGH,
            EvidenceKind.KNOWN_ROOT_HEURISTIC,
            Actionability.AI_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.NONE,
            f"{rule.label}：用户产生的数据，由用户决定；历史分组 {bucket}",
            (*common_tags, "user_decision", f"age_bucket:{bucket}"),
        )

    if decision.action is PolicyAction.TOOL_DELETE:
        threshold = decision.effective_idle_days
        threshold_text = "未知" if threshold is None else f"{threshold:g} 天"
        guard = "；执行前必须确认 Codex 已关闭" if rule.requires_process_closed else ""
        guard_tags = ("requires_process_closed",) if rule.requires_process_closed else ()
        return _Classification(
            category,
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.AGE_AND_APPROVED_ROOT,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            (
                f"{rule.label}：可再生数据，达到 {threshold_text} 闲置阈值，"
                f"删除收益评分 {decision.benefit_score}/100{guard}"
            ),
            (
                *common_tags,
                "tool_decision",
                "regenerable",
                f"benefit:{decision.benefit_score}",
                *guard_tags,
            ),
        )

    reason_by_action = {
        PolicyAction.TOOL_KEEP_RECENT: "近期仍有复用价值，暂不清理",
        PolicyAction.TOOL_KEEP_LOW_BENEFIT: "可再生但释放空间太小，不值得制造重建或下载开销",
        PolicyAction.TOOL_KEEP_IN_USE: "应用仍在运行，避免清理正在写入的数据",
        PolicyAction.TOOL_KEEP_UNKNOWN_USAGE: "缺少可靠的最近使用时间，暂不自动清理",
    }
    return _Classification(
        category,
        ReviewLane.REPORT_ONLY,
        RiskTier.LOW,
        EvidenceKind.KNOWN_ROOT_HEURISTIC,
        Actionability.REPORT_ONLY,
        ExecutionPolicy.NONE,
        RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
        f"{rule.label}：{reason_by_action[decision.action]}",
        (*common_tags, "tool_keep", decision.action.value.casefold()),
    )


def _is_descendant(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root)))
        ) == os.path.normcase(os.path.abspath(root))
    except ValueError:
        return False


def is_regenerable_byproduct(path: Path, config: DeleteClassification) -> bool:
    """Return whether *path* is output a program writes and rewrites by itself."""

    name = path.name.casefold()
    if path.suffix.casefold() in config.byproduct_suffixes:
        return True
    stem = Path(name).stem.casefold()
    if Path(stem).suffix.casefold() in config.byproduct_suffixes:
        return True
    return bool({part.casefold() for part in path.parts} & config.byproduct_segments)


def _version_key(name: str, separator_regex: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(separator_regex, name, flags=re.IGNORECASE):
        if chunk.isdigit():
            parts.append(int(chunk))
    return tuple(parts)


def _is_aged_temp_child(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    old_temp_days: int,
) -> bool:
    parent = _normalized_path(path.parent)
    if parent not in _age_based_known_roots(known_roots):
        return False
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    return (datetime.now(UTC).timestamp() - modified) > timedelta(
        days=old_temp_days
    ).total_seconds()


@lru_cache(maxsize=8)
def _age_based_known_roots(
    known_roots: tuple[KnownCleanupRoot, ...],
) -> frozenset[str]:
    return frozenset(
        _normalized_path(root.path)
        for root in known_roots
        if root.policy is CleanupPolicy.AGE_BASED_REVIEW
    )


def _newest_version_sibling(
    parent: str, version_name_regex: str, version_separators_regex: str
) -> str | None:
    try:
        with os.scandir(parent) as entries:
            names = [
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and re.fullmatch(version_name_regex, entry.name, flags=re.IGNORECASE)
            ]
    except OSError:
        return None
    if len(names) < 2:
        return None
    return max(names, key=lambda name: _version_key(name, version_separators_regex))


def is_stale_version_directory(path: Path, config: DeleteClassification) -> bool:
    if not re.fullmatch(config.version_name_regex, path.name, flags=re.IGNORECASE):
        return False
    if path.parent.name.casefold() not in config.self_updater_parents:
        return False
    newest = _newest_version_sibling(
        str(path.parent), config.version_name_regex, config.version_separators_regex
    )
    return newest is not None and path.name.casefold() != newest.casefold()


def is_inside_cache_directory(path: Path, config: DeleteClassification) -> bool:
    return bool(
        {part.casefold() for part in path.parent.parts} & config.cache_directory_names
    )


def is_program_payload_file(path: Path, config: KeepClassification) -> bool:
    return path.suffix.casefold() in config.program_payload_suffixes


def is_installed_addon_payload(path: Path, config: KeepClassification) -> bool:
    return bool(
        {part.casefold() for part in path.parts} & config.installed_payload_segments
    )


def is_application_state_file(path: Path, config: KeepClassification) -> bool:
    name = path.name.casefold()
    if path.suffix.casefold() in config.application_state_suffixes:
        return True
    if name in config.application_state_names:
        return True
    return any(name.endswith(tail) for tail in config.application_state_tails)


def is_regenerable_tool_directory(path: Path, config: DeleteClassification) -> bool:
    return path.name.casefold() in config.regenerable_tool_directories


def directory_cleanup_scope(
    path: Path,
    known_roots: tuple[KnownCleanupRoot, ...],
    delete_config: DeleteClassification,
    keep_config: KeepClassification,
) -> DirectoryScope:
    is_known_root = _normalized_path(path) in _whole_tree_known_roots(known_roots)
    is_tool_output = is_regenerable_tool_directory(path, delete_config)
    if not is_known_root and not is_tool_output:
        if _is_aged_temp_child(path, known_roots, delete_config.old_temp_days):
            return DirectoryScope.AGED_TEMP_ITEM
        if is_stale_version_directory(path, delete_config):
            return DirectoryScope.STALE_VERSION
        return DirectoryScope.NOT_ELIGIBLE
    if is_known_root:
        return DirectoryScope.KNOWN_CACHE_ROOT
    if any(is_regenerable_tool_directory(parent, delete_config) for parent in path.parents):
        return DirectoryScope.NOT_ELIGIBLE
    if known_root_for_path(path, known_roots) is not None:
        return DirectoryScope.NOT_ELIGIBLE
    if _is_application_payload(path, keep_config):
        return DirectoryScope.NOT_ELIGIBLE
    return DirectoryScope.REGENERABLE_TOOL_OUTPUT


def _is_application_payload(path: Path, config: KeepClassification) -> bool:
    return bool(
        {part.casefold() for part in path.parts} & config.application_data_segments
    )


def _normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


@lru_cache(maxsize=8)
def _whole_tree_known_roots(known_roots: tuple[KnownCleanupRoot, ...]) -> frozenset[str]:
    return frozenset(
        _normalized_path(root.path)
        for root in known_roots
        if root.policy is not CleanupPolicy.AGE_BASED_REVIEW
    )


def is_development_cache_hint(path: Path, config: DeleteClassification) -> bool:
    return bool(
        {part.casefold() for part in path.parts} & config.development_cache_segments
    )


def _infer_presentation_category(
    path: Path, config: DeleteClassification
) -> CleanupCategory:
    parts = {part.casefold() for part in path.parts}
    suffix = path.suffix.casefold()
    if parts & config.windows_update_segments or any(
        group.issubset(parts) for group in config.windows_update_segment_groups
    ):
        return CleanupCategory.WINDOWS_UPDATE
    if parts & config.container_segments or suffix in config.container_suffixes:
        return CleanupCategory.CONTAINER_STORAGE
    if parts & config.conda_segments:
        return CleanupCategory.CONDA_CACHE
    if parts & config.ide_segments and parts & config.cache_segments:
        return CleanupCategory.IDE_CACHE
    if parts & config.build_segments:
        return CleanupCategory.PROJECT_BUILD_OUTPUT
    if suffix in config.system_log_suffixes:
        return CleanupCategory.SYSTEM_LOGS
    if parts & config.downloads_segments or suffix in config.installer_suffixes:
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
    "CleanupTargetKind",
    "DirectoryScope",
    "DirectorySubtreeTotals",
    "EvidenceKind",
    "ExecutionPolicy",
    "RecoveryCapability",
    "ReviewLane",
    "RiskTier",
    "TriageItem",
    "TriageSession",
    "directory_cleanup_scope",
    "is_application_state_file",
    "is_development_cache_hint",
    "is_inside_cache_directory",
    "is_installed_addon_payload",
    "is_program_payload_file",
    "is_regenerable_byproduct",
    "is_regenerable_tool_directory",
    "is_stale_version_directory",
    "triage_directory",
    "triage_file",
]
