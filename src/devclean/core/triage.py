"""Read-only result classification for the scan -> review -> cleanup workflow.

Classification describes evidence and review priority. It never selects an
item, never grants execution authority, and never invokes an AI or cleanup
operation. This separation is a permanent safety invariant.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from heapq import heappush, heapreplace
from pathlib import Path

from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    application_display_name,
    evaluate_application_path,
    file_time_utc,
    must_close_before_cleanup,
    snapshot_roots,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    KnownCleanupRoot,
    SourceDomain,
    known_root_for_path,
    source_domain_for_category,
)
from devclean.core.installer_files import (
    has_installed_program_markers,
    identify_installer_package,
    installed_program_owner,
    looks_like_uninstaller,
    program_location,
    system_installer_cache,
)
from devclean.core.system_anchor import system_anchor_refusal
from devclean.core.temp_folders import temp_folder_kind
from devclean.core.user_rules import (
    DeleteClassification,
    KeepClassification,
    RuleDecision,
    UserRules,
    normalise_path,
)
from devclean.platform.windows.known_folders import downloads_path
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
    # A temporary folder emptied as one item (see devclean.core.temp_folders).
    TEMP_FOLDER = "TEMP_FOLDER"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class ReviewLane(StrEnum):
    """Local confidence lanes; none implies automatic execution."""

    DETERMINISTIC_CANDIDATE = "DETERMINISTIC_CANDIDATE"
    USER_REVIEW = "USER_REVIEW"
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
    USER_REVIEW = "USER_REVIEW"
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


def _has_audited_vendor_authority(root: KnownCleanupRoot) -> bool:
    """Return whether a vendor root carries its exact audited mutation rule."""

    rule = root.application_rule
    return (
        root.policy is CleanupPolicy.VENDOR_MANAGED
        and rule is not None
        and rule.owner is DecisionOwner.TOOL
        and rule.allow_whole_tree
    )


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

    def observe_path(
        self,
        path: str,
        rules: UserRules,
        *,
        target_kind: CleanupTargetKind = CleanupTargetKind.FILE,
    ) -> None:
        """Retain observed FILE/DIRECTORY paths protected by their own rule kind."""

        if self._observation_rules is None:
            self._observation_rules = rules
        elif self._observation_rules is not rules:
            raise ValueError("one scan session must use one pinned rule set")
        decision = (
            rules.directory_decision_for(path)
            if target_kind is CleanupTargetKind.DIRECTORY
            else rules.decision_for(path)
        )
        if decision is RuleDecision.KEEP:
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

        return (entry[2] for bucket in self._items.values() for entry in bucket)

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
            if (
                rules.directory_decision_for(item.path)
                if item.target_kind is CleanupTargetKind.DIRECTORY
                else rules.decision_for(item.path)
            )
            is RuleDecision.KEEP
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
    download_roots: tuple[Path, ...] | None = None,
) -> TriageItem:
    """Classify one file observation without mutating state.

    Contents are read only for executables and installer packages, and only a
    bounded header, to tell an installer apart from a program file.
    """

    if record.kind is not ScanRecordKind.FILE:
        raise ValueError("triage accepts file observations only")
    classification = _classify(
        Path(record.path),
        record.logical_size,
        record.last_write_time_ns,
        now=now,
        temp_root=temp_root,
        known_roots=known_roots,
        download_roots=user_download_roots() if download_roots is None else download_roots,
        delete_config=delete_config,
        keep_config=keep_config,
    )
    if system_anchor_refusal(Path(record.path), known_roots, keep_config) is not None:
        classification = _inside_system_location(classification)
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
        audited_vendor = _has_audited_vendor_authority(known)
        recovery = (
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT
            if audited_vendor
            else RecoveryCapability.UNKNOWN
        )
        if audited_vendor:
            lane = ReviewLane.DETERMINISTIC_CANDIDATE
            risk_tier = RiskTier.LOW
            evidence_kind = EvidenceKind.KNOWN_ROOT_HEURISTIC
            actionability = Actionability.REVIEW_PLAN
            execution_policy = ExecutionPolicy.USER_CHOICE_DELETE
            reason = f"{known.label}：精确匹配已审计的厂商管理根目录，可作为单个对象清理"
            tags = ("whole_directory", "known_cache_root", "tool_decision")
        elif known.policy is CleanupPolicy.REPORT_ONLY:
            lane = ReviewLane.REPORT_ONLY
            risk_tier = RiskTier.PROTECTED
            evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION
            actionability = Actionability.REPORT_ONLY
            execution_policy = ExecutionPolicy.NONE
            reason = f"{known.label}：已知受保护目录，只生成报告"
            tags = ("whole_directory", "known_cache_root", "report_only")
        else:
            lane = ReviewLane.REPORT_ONLY
            risk_tier = RiskTier.PROTECTED
            evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION
            actionability = Actionability.REPORT_ONLY
            execution_policy = ExecutionPolicy.NONE
            reason = f"{known.label}：没有足够的通用删除契约，工具直接保护，不要求用户猜测"
            tags = ("whole_directory", "known_cache_root", "report_only")
    elif scope is DirectoryScope.AGED_TEMP_ITEM:
        category = CleanupCategory.USER_TEMP
        recovery = RecoveryCapability.UNKNOWN
        lane = ReviewLane.REPORT_ONLY
        risk_tier = RiskTier.PROTECTED
        evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION
        actionability = Actionability.REPORT_ONLY
        execution_policy = ExecutionPolicy.NONE
        reason = f"{path.name}：文件时间只能说明观察到的年龄，不能证明整个临时目录当前可删"
        tags = ("whole_directory", "aged_temp_item", "report_only")
    elif scope is DirectoryScope.STALE_VERSION:
        category = CleanupCategory.OTHER
        recovery = RecoveryCapability.NONE
        lane = ReviewLane.REPORT_ONLY
        risk_tier = RiskTier.PROTECTED
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.REPORT_ONLY
        execution_policy = ExecutionPolicy.NONE
        reason = f"{path.name}：名称像旧版本但缺少厂商生命周期证据，工具直接保护"
        tags = ("whole_directory", "stale_version", "report_only")
    else:
        category = CleanupCategory.PROJECT_BUILD_OUTPUT
        recovery = RecoveryCapability.NONE
        lane = ReviewLane.REPORT_ONLY
        risk_tier = RiskTier.PROTECTED
        evidence_kind = EvidenceKind.PATH_HEURISTIC
        actionability = Actionability.REPORT_ONLY
        execution_policy = ExecutionPolicy.NONE
        reason = f"{path.name}：名称像可重建工具产物，但项目语义未经证明，工具直接保护"
        tags = ("whole_directory", "regenerable_tool_output", "report_only")
    if (
        lane is not ReviewLane.REPORT_ONLY
        and system_anchor_refusal(path, known_roots, keep_config) is not None
    ):
        lane = ReviewLane.REPORT_ONLY
        risk_tier = RiskTier.PROTECTED
        evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION
        actionability = Actionability.REPORT_ONLY
        execution_policy = ExecutionPolicy.NONE
        reason = f"{known.label if known is not None else path.name}：{_SYSTEM_LOCATION_REASON}"
        tags = ("whole_directory", "system_location", "report_only")
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=0,
        allocated_size=None,
        category=category,
        source_domain=source_domain_for_category(category, delete_config.category_source_domains),
        lane=lane,
        risk_tier=risk_tier,
        evidence_kind=evidence_kind,
        actionability=actionability,
        execution_policy=execution_policy,
        recovery=recovery,
        reason=reason,
        tags=tags,
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=scope,
    )


def triage_temp_folder(
    record: ScanRecord,
    *,
    delete_config: DeleteClassification,
    kept_out: int = 0,
) -> TriageItem:
    """One temporary folder as a single "safe to clean" item (a user decision).

    Its size comes from the scan listing; its files were not classified.
    Cleaning empties it and keeps the folder itself.
    """

    if record.kind is not ScanRecordKind.DIRECTORY:
        raise ValueError("a temp folder item needs the folder's directory observation")
    reason = (
        "临时文件夹：按你的决定整体清理其中内容，保留文件夹本身；"
        "清理时正被程序使用的文件会跳过并说明"
    )
    if kept_out:
        reason += f"；其中 {kept_out:,} 项由应用规则或你的保留设置单独处理，不随文件夹清理"
    category = CleanupCategory.USER_TEMP
    return TriageItem(
        record=record,
        path=record.path,
        logical_size=0,
        allocated_size=None,
        category=category,
        source_domain=source_domain_for_category(category, delete_config.category_source_domains),
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk_tier=RiskTier.LOW,
        evidence_kind=EvidenceKind.FILESYSTEM_OBSERVATION,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.NONE,
        reason=reason,
        tags=("whole_directory", "temp_folder", "user_decision_temp_folder"),
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=DirectoryScope.TEMP_FOLDER,
    )


_SYSTEM_LOCATION_REASON = (
    "位于 Windows 系统保护位置（Windows、Program Files、ProgramData 等），"
    "删除前的系统目录检查不允许在这里删除，所以不列为可清理"
)


def _inside_system_location(classification: _Classification) -> _Classification:
    """Offer nothing that the deletion step's system-root deny-list would refuse."""

    if (
        classification.lane is ReviewLane.REPORT_ONLY
        and "tool_keep_in_use" not in classification.tags
    ):
        # Already protected; the tag keeps AI routing and learned rules away too.
        # A running program is only a passing reason: the deny-list replaces it.
        return replace(classification, tags=(*classification.tags, "system_location"))
    return _Classification(
        classification.category,
        ReviewLane.REPORT_ONLY,
        RiskTier.PROTECTED,
        EvidenceKind.FILESYSTEM_OBSERVATION,
        Actionability.REPORT_ONLY,
        ExecutionPolicy.NONE,
        RecoveryCapability.NONE,
        _SYSTEM_LOCATION_REASON,
        ("system_location", "report_only"),
    )


def _classify(
    path: Path,
    logical_size: int,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    temp_root: Path | None,
    known_roots: tuple[KnownCleanupRoot, ...],
    download_roots: tuple[Path, ...],
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
    installer = _installer_file_classification(
        path,
        known_roots=known_roots,
        temp_root=temp_root,
        download_roots=download_roots,
        delete_config=delete_config,
    )
    if installer is not None:
        return installer
    if is_program_payload_file(path, keep_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.MEDIUM,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            (
                "程序文件、库文件或虚拟磁盘：被程序直接运行、加载或挂载，"
                "没有安装包特征；删除会让对应程序或虚拟机无法使用"
            ),
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

    known = known_root_for_path(path, known_roots)
    if known is not None:
        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.UNKNOWN,
                (
                    f"{known.label}：旧规则曾按文件时间授予删除权限；"
                    "当前没有来源证明单靠 mtime 能代表未使用或安全删除，工具直接保护"
                ),
                ("known_root", "legacy_age_based_review", "report_only"),
            )
        if known.policy is CleanupPolicy.VENDOR_MANAGED:
            provenance = (
                "audited_whole_tree_root"
                if _has_audited_vendor_authority(known)
                else "missing_application_rule"
            )
            return _Classification(
                known.category,
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.NONE,
                (
                    f"{known.label}：目录级厂商规则不能替代具体文件语义；"
                    "应用分类器未确认此文件，工具直接保护"
                ),
                ("known_root", "vendor_managed", provenance, "unmatched_file", "report_only"),
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
                ReviewLane.REPORT_ONLY,
                RiskTier.PROTECTED,
                EvidenceKind.FILESYSTEM_OBSERVATION,
                Actionability.REPORT_ONLY,
                ExecutionPolicy.NONE,
                RecoveryCapability.NONE,
                f"{known.label}：旧配置要求人工判断，但没有通用删除契约；工具直接保护",
                ("known_root", "manual_review", "report_only"),
            )

    root = temp_root or Path(tempfile.gettempdir())
    if _is_descendant(path, root):
        return _Classification(
            CleanupCategory.USER_TEMP,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.FILESYSTEM_OBSERVATION,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.UNKNOWN,
            (
                "位于当前用户临时目录，但 Windows 的维护语义是清理未在使用的临时文件；"
                "单独的 mtime/年龄不能证明当前未使用，工具直接保护"
            ),
            # Not "unknown": a temp file is a known kind that stays protected,
            # exactly like the catalogued user-temp root, so neither the AI lane
            # nor a learned file rule can reach it.
            ("temp_root", "report_only"),
        )

    if is_regenerable_byproduct(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "后缀或目录名像日志、转储或临时产物，但名称不能证明生命周期；工具直接保护",
            ("byproduct", "report_only"),
        )

    if is_inside_cache_directory(path, delete_config):
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "目录名看起来像缓存，但通用 cache 名称不能证明删除边界；工具直接保护",
            ("cache_directory", "report_only"),
        )

    if is_development_cache_hint(path, delete_config):
        category = _infer_presentation_category(path, delete_config)
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "路径看起来像开发缓存，但缺少厂商精确证据；工具直接保护，不默认花费 AI",
            ("path_heuristic", "report_only"),
        )

    category = _infer_presentation_category(path, delete_config)
    if category.value in delete_config.inferred_ai_review_categories:
        return _Classification(
            category,
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.PATH_HEURISTIC,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            "旧规则把该类别送 AI，但类别推断不能证明删除安全；工具直接保护",
            ("path_heuristic", "report_only"),
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
        ReviewLane.REPORT_ONLY,
        RiskTier.PROTECTED,
        EvidenceKind.FILESYSTEM_OBSERVATION,
        Actionability.REPORT_ONLY,
        ExecutionPolicy.NONE,
        RecoveryCapability.NONE,
        "本工具无法确定这是什么；未知不等于可删，工具直接保护",
        ("unknown", "report_only"),
    )


def _application_classification(
    path: Path,
    logical_size: int,
    last_write_time_ns: int | None,
    *,
    now: datetime | None,
    delete_config: DeleteClassification,
) -> _Classification | None:
    last_used = file_time_utc(last_write_time_ns)
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
    elif "log" in rule.rule_id or "debug" in rule.rule_id:
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
            ReviewLane.USER_REVIEW,
            RiskTier.MEDIUM,
            EvidenceKind.KNOWN_ROOT_HEURISTIC,
            Actionability.USER_REVIEW,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.NONE,
            f"{rule.label}：用户产生的数据，由用户决定；历史分组 {bucket}",
            (*common_tags, "user_decision", "user_review", f"age_bucket:{bucket}"),
        )

    if decision.action is PolicyAction.TOOL_DELETE:
        threshold = decision.effective_idle_days
        threshold_text = "未知" if threshold is None else f"{threshold:g} 天"
        app_name = application_display_name(rule.app_id)
        must_close = must_close_before_cleanup(rule)
        guard = f"；执行前必须确认 {app_name} 已关闭" if must_close else ""
        guard_tags = ("requires_process_closed",) if must_close else ()
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
    return bool({part.casefold() for part in path.parent.parts} & config.cache_directory_names)


def is_program_payload_file(path: Path, config: KeepClassification) -> bool:
    return path.suffix.casefold() in config.program_payload_suffixes


@snapshot_roots
def user_download_roots(environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """The current user's Downloads folder, including a redirected Known Folder."""

    source = os.environ if environment is None else environment
    profile = next((value for key, value in source.items() if key.casefold() == "userprofile"), "")
    roots: list[Path] = []
    if profile and os.path.isabs(profile):
        roots.append(Path(profile) / "Downloads")
    if os.name == "nt" and environment is None:
        with suppress(OSError, RuntimeError):
            roots.append(downloads_path())
    return tuple(dict.fromkeys(roots))


def _installer_file_classification(
    path: Path,
    *,
    known_roots: tuple[KnownCleanupRoot, ...],
    temp_root: Path | None,
    download_roots: tuple[Path, ...],
    delete_config: DeleteClassification,
) -> _Classification | None:
    """Classify executables, installer packages and disk images by what they are.

    An installer package is deletable wherever it sits, at any folder depth,
    unless an installed program, Windows or a Windows installer cache owns it.
    An executable without installer evidence, in any scanned folder, is
    uncertain: the AI or the user decides, and nothing is deleted before that.
    Returns ``None`` when catalogued roots and temp folders keep their own
    policy.
    """

    suffix = path.suffix.casefold()
    if suffix != ".exe" and suffix not in delete_config.installer_suffixes:
        return None
    if known_root_for_path(path, known_roots) is not None:
        return None
    if _is_descendant(path, temp_root or Path(tempfile.gettempdir())):
        return None
    refusal = installer_ownership(path)
    if refusal is not None:
        reason, tag = refusal
        return _Classification(
            _infer_presentation_category(path, delete_config),
            ReviewLane.REPORT_ONLY,
            RiskTier.PROTECTED,
            EvidenceKind.FILESYSTEM_OBSERVATION,
            Actionability.REPORT_ONLY,
            ExecutionPolicy.NONE,
            RecoveryCapability.NONE,
            reason,
            (tag, "report_only"),
        )
    package = identify_installer_package(path)
    if package is not None:
        return _Classification(
            CleanupCategory.INSTALLERS_DOWNLOADS,
            ReviewLane.DETERMINISTIC_CANDIDATE,
            RiskTier.LOW,
            EvidenceKind.FILESYSTEM_OBSERVATION,
            Actionability.REVIEW_PLAN,
            ExecutionPolicy.USER_CHOICE_DELETE,
            RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
            (
                f"安装包（{package.description}）：只用于安装软件，不属于任何已安装程序；"
                "删除只移除这个文件，不会卸载已安装的软件，需要时可重新下载"
            ),
            ("installer_package", f"installer_format:{package.kind}"),
        )
    downloaded = any(_is_descendant(path, root) for root in download_roots)
    category = (
        CleanupCategory.INSTALLERS_DOWNLOADS
        if downloaded or suffix != ".exe"
        else _infer_presentation_category(path, delete_config)
    )
    if suffix == ".iso":
        reason = "光盘镜像：可能是系统或软件的安装介质，也可能正被虚拟机挂载；需要你或 AI 判断"
    elif suffix == ".exe":
        reason = (
            f"{'下载的' if downloaded else ''}可执行文件，没有发现安装包特征"
            "（安装框架标记或版本信息），也不属于已安装程序或系统："
            "可能是便携程序、工具或构建产物；需要你或 AI 判断"
        )
    else:
        reason = "扩展名是安装包，但内容不是有效的安装包格式；需要你或 AI 判断"
    return _Classification(
        category,
        ReviewLane.AI_REVIEW,
        RiskTier.MEDIUM,
        EvidenceKind.FILESYSTEM_OBSERVATION,
        Actionability.AI_REVIEW,
        ExecutionPolicy.USER_CHOICE_DELETE,
        RecoveryCapability.UNKNOWN,
        reason,
        ("installer_uncertain", "ai_review_candidate"),
    )


def installer_ownership(path: Path) -> tuple[str, str] | None:
    """Return (reason, tag) when an installed program or system cache owns *path*.

    Checked at scan time and again just before deletion.
    """

    cache = system_installer_cache(path)
    if cache is not None:
        return (
            f"{cache}：已安装软件修复和卸载要用到其中的安装包，不能删除",
            "system_installer_cache",
        )
    location = program_location(path)
    if location is not None:
        return (
            f"位于{location}：这里的程序由 Windows 或安装程序管理，删除会损坏它们",
            "installed_program",
        )
    owner = installed_program_owner(path)
    if owner is not None:
        return (
            f"已安装程序「{owner}」的文件（Windows 已安装程序列表登记的位置）："
            "删除会影响它的运行、修复或卸载",
            "installed_program",
        )
    if looks_like_uninstaller(path) or has_installed_program_markers(path.parent):
        return (
            "已安装程序的卸载程序或安装文件夹（旁边有安装程序留下的卸载数据）："
            "删除会让该程序无法正常卸载或修复",
            "installed_program",
        )
    if getattr(sys, "frozen", False) and _normalized_path(path) == _normalized_path(
        Path(sys.executable)
    ):
        return ("正在运行的 DevClean 程序本身", "program_payload")
    return None


def is_installed_addon_payload(path: Path, config: KeepClassification) -> bool:
    return bool({part.casefold() for part in path.parts} & config.installed_payload_segments)


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
    if temp_folder_kind(path, known_roots, keep_config) is not None:
        return DirectoryScope.TEMP_FOLDER
    is_known_root = _normalized_path(path) in _whole_tree_known_roots(known_roots)
    is_tool_output = is_regenerable_tool_directory(path, delete_config)
    if not is_known_root and not is_tool_output:
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
    return bool({part.casefold() for part in path.parts} & config.application_data_segments)


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
    return bool({part.casefold() for part in path.parts} & config.development_cache_segments)


def _infer_presentation_category(path: Path, config: DeleteClassification) -> CleanupCategory:
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


def _is_older_than(last_write_time_ns: int | None, age: timedelta, now: datetime | None) -> bool:
    observed = file_time_utc(last_write_time_ns)
    if observed is None:
        return False
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
    "installer_ownership",
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
    "triage_temp_folder",
    "user_download_roots",
]
