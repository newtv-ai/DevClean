"""Versioned, serialization-friendly domain models.

The current milestone is inventory-only. Models include future action and plan shapes so their
security semantics can be reviewed before any executor exists.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

SCHEMA_VERSION = "1.0.0"

_GENERIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_ADAPTER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ACTION_ID_PATTERN = re.compile(r"^action_[a-f0-9]{32}$")
_PLAN_ID_PATTERN = re.compile(r"^plan_[a-f0-9]{32}$")
_ID_PREFIX_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _require_string(
    value: object,
    field_name: str,
    *,
    min_length: int = 0,
    max_length: int,
    pattern: re.Pattern[str] | None = None,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not min_length <= len(value) <= max_length:
        raise ValueError(
            f"{field_name} length must be between {min_length} and {max_length}"
        )
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")


def _require_optional_string(
    value: object,
    field_name: str,
    *,
    max_length: int,
) -> None:
    if value is not None:
        _require_string(value, field_name, max_length=max_length)


def _require_string_items(
    values: tuple[str, ...],
    field_name: str,
    *,
    max_items: int,
    item_max_length: int,
) -> None:
    if len(values) > max_items:
        raise ValueError(f"{field_name} must contain at most {max_items} items")
    for index, value in enumerate(values):
        _require_string(
            value,
            f"{field_name}[{index}]",
            min_length=1,
            max_length=item_max_length,
        )


def _require_optional_non_negative_integer(value: object, field_name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"{field_name} must be a non-negative integer or null")


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field_name} must be a {enum_type.__name__} value")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


class SemanticType(StrEnum):
    REBUILDABLE_CACHE = "REBUILDABLE_CACHE"
    INSTALLED_MODEL = "INSTALLED_MODEL"
    PACKAGE_STORE = "PACKAGE_STORE"
    BUILD_OUTPUT = "BUILD_OUTPUT"
    APP_STATE = "APP_STATE"
    USER_DATA = "USER_DATA"
    SYSTEM_ROLLBACK = "SYSTEM_ROLLBACK"
    UNKNOWN = "UNKNOWN"


class RiskTier(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class UndoCapability(StrEnum):
    RECYCLE_BIN = "RECYCLE_BIN"
    VENDOR_ROLLBACK = "VENDOR_ROLLBACK"
    NONE = "NONE"


class Reconstruction(StrEnum):
    EXACT = "EXACT"
    REDOWNLOAD_BEST_EFFORT = "REDOWNLOAD_BEST_EFFORT"
    REBUILD_BEST_EFFORT = "REBUILD_BEST_EFFORT"
    NONE = "NONE"


class ProvenanceClass(StrEnum):
    REGENERABLE_CONFIRMED = "REGENERABLE_CONFIRMED"
    REMOTE_RETRIEVABLE_VERIFIED = "REMOTE_RETRIEVABLE_VERIFIED"
    LOCAL_ONLY = "LOCAL_ONLY"
    UNKNOWN = "UNKNOWN"


class Confidence(StrEnum):
    EXACT = "EXACT"
    ESTIMATE = "ESTIMATE"
    UNKNOWN = "UNKNOWN"


class EffectClass(StrEnum):
    PURE_QUERY = "PURE_QUERY"
    OBSERVATION_WITH_OPERATIONAL_WRITES = "OBSERVATION_WITH_OPERATIONAL_WRITES"
    MAINTENANCE = "MAINTENANCE"
    DESTRUCTIVE = "DESTRUCTIVE"


class SelectionMode(StrEnum):
    EXACT_IDS = "EXACT_IDS"
    POLICY_GC = "POLICY_GC"
    WHOLE_CACHE = "WHOLE_CACHE"
    NONE = "NONE"


class PreviewMode(StrEnum):
    VENDOR_EXACT = "VENDOR_EXACT"
    VENDOR_ESTIMATE = "VENDOR_ESTIMATE"
    INTERNAL_SNAPSHOT = "INTERNAL_SNAPSHOT"
    NONE = "NONE"


class ReclaimScope(StrEnum):
    HOST_PHYSICAL = "HOST_PHYSICAL"
    VENDOR_LOGICAL = "VENDOR_LOGICAL"
    BOTH = "BOTH"
    UNKNOWN = "UNKNOWN"


class ActionKind(StrEnum):
    EXACT_VENDOR_ACTION = "EXACT_VENDOR_ACTION"
    PREVIEWED_VENDOR_POLICY = "PREVIEWED_VENDOR_POLICY"
    UNPREVIEWABLE_VENDOR_POLICY = "UNPREVIEWABLE_VENDOR_POLICY"
    DIRECT_FS_ACTION = "DIRECT_FS_ACTION"
    FIXED_BROKER_ACTION = "FIXED_BROKER_ACTION"
    REPORT_ONLY = "REPORT_ONLY"


class ScanStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SizeValue:
    value: int | None
    confidence: Confidence

    def __post_init__(self) -> None:
        _require_enum(self.confidence, Confidence, "confidence")
        _require_optional_non_negative_integer(self.value, "value")
        if self.value is None and self.confidence is not Confidence.UNKNOWN:
            raise ValueError("a missing size must use UNKNOWN confidence")


@dataclass(frozen=True, slots=True)
class FileIdentity:
    volume_serial: str | None = None
    file_id: str | None = None
    file_id_kind: str | None = None
    link_count: int | None = None
    attributes: int | None = None
    reparse_tag: int | None = None
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None

    def __post_init__(self) -> None:
        _require_optional_string(self.volume_serial, "volume_serial", max_length=128)
        _require_optional_string(self.file_id, "file_id", max_length=256)
        _require_optional_string(self.file_id_kind, "file_id_kind", max_length=64)
        for field_name in (
            "link_count",
            "attributes",
            "reparse_tag",
            "creation_time_ns",
            "last_write_time_ns",
        ):
            _require_optional_non_negative_integer(getattr(self, field_name), field_name)

    @property
    def stable_key(self) -> tuple[str, str] | None:
        if self.volume_serial and self.file_id:
            return (self.volume_serial, self.file_id)
        return None


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    detail: str
    checked_at: datetime
    digest: str | None = None

    def __post_init__(self) -> None:
        _require_string(self.source, "source", min_length=1, max_length=1024)
        _require_string(self.detail, "detail", min_length=1, max_length=8192)
        _require_aware_datetime(self.checked_at, "checked_at")
        _require_optional_string(self.digest, "digest", max_length=256)


@dataclass(frozen=True, slots=True)
class Resource:
    candidate_id: str
    adapter_id: str
    display_name: str
    semantic_type: SemanticType
    risk_tier: RiskTier
    provenance_class: ProvenanceClass
    vendor_locator: str | None = None
    path: str | None = None
    logical_size: SizeValue = field(
        default_factory=lambda: SizeValue(None, Confidence.UNKNOWN)
    )
    allocated_size: SizeValue = field(
        default_factory=lambda: SizeValue(None, Confidence.UNKNOWN)
    )
    exclusive_host_reclaimable: SizeValue = field(
        default_factory=lambda: SizeValue(None, Confidence.UNKNOWN)
    )
    vendor_logical_reclaimable: SizeValue = field(
        default_factory=lambda: SizeValue(None, Confidence.UNKNOWN)
    )
    undo_capability: UndoCapability = UndoCapability.NONE
    reconstruction: Reconstruction = Reconstruction.NONE
    reconstruction_preconditions: tuple[str, ...] = ()
    identity: FileIdentity | None = None
    warnings: tuple[str, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    actionable: bool = False

    def __post_init__(self) -> None:
        _require_string(
            self.candidate_id,
            "candidate_id",
            min_length=1,
            max_length=256,
            pattern=_GENERIC_ID_PATTERN,
        )
        _require_string(
            self.adapter_id,
            "adapter_id",
            min_length=1,
            max_length=64,
            pattern=_ADAPTER_ID_PATTERN,
        )
        _require_string(self.display_name, "display_name", min_length=1, max_length=512)
        _require_enum(self.semantic_type, SemanticType, "semantic_type")
        _require_enum(self.risk_tier, RiskTier, "risk_tier")
        _require_enum(self.provenance_class, ProvenanceClass, "provenance_class")
        _require_enum(self.undo_capability, UndoCapability, "undo_capability")
        _require_enum(self.reconstruction, Reconstruction, "reconstruction")
        _require_optional_string(self.vendor_locator, "vendor_locator", max_length=32767)
        _require_optional_string(self.path, "path", max_length=32767)
        _require_string_items(
            self.reconstruction_preconditions,
            "reconstruction_preconditions",
            max_items=64,
            item_max_length=1024,
        )
        _require_string_items(
            self.warnings,
            "warnings",
            max_items=256,
            item_max_length=2048,
        )
        if len(self.evidence) > 256:
            raise ValueError("evidence must contain at most 256 items")
        if any(not isinstance(item, Evidence) for item in self.evidence):
            raise ValueError("evidence items must be Evidence values")
        if self.identity is not None and not isinstance(self.identity, FileIdentity):
            raise ValueError("identity must be a FileIdentity value or null")
        for field_name in (
            "logical_size",
            "allocated_size",
            "exclusive_host_reclaimable",
            "vendor_logical_reclaimable",
        ):
            if not isinstance(getattr(self, field_name), SizeValue):
                raise ValueError(f"{field_name} must be a SizeValue")
        if not isinstance(self.actionable, bool):
            raise ValueError("actionable must be a boolean")
        if self.semantic_type is SemanticType.UNKNOWN:
            if self.risk_tier is not RiskTier.RED:
                raise ValueError("UNKNOWN resources must be RED")
            if self.actionable:
                raise ValueError("UNKNOWN resources cannot be actionable")
        if (
            self.provenance_class in {ProvenanceClass.LOCAL_ONLY, ProvenanceClass.UNKNOWN}
            and self.actionable
        ):
            raise ValueError("LOCAL_ONLY and UNKNOWN resources cannot be actionable")

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(frozen=True, slots=True)
class Action:
    action_id: str
    candidate_id: str
    adapter_id: str
    kind: ActionKind
    effect_class: EffectClass
    selection_mode: SelectionMode
    preview_mode: PreviewMode
    reclaim_scope: ReclaimScope
    risk_tier: RiskTier
    summary: str
    undo_capability: UndoCapability = UndoCapability.NONE
    reconstruction: Reconstruction = Reconstruction.NONE
    enabled: bool = False

    def __post_init__(self) -> None:
        _require_string(
            self.action_id,
            "action_id",
            min_length=39,
            max_length=39,
            pattern=_ACTION_ID_PATTERN,
        )
        _require_string(
            self.candidate_id,
            "candidate_id",
            min_length=1,
            max_length=256,
            pattern=_GENERIC_ID_PATTERN,
        )
        _require_string(
            self.adapter_id,
            "adapter_id",
            min_length=1,
            max_length=64,
            pattern=_ADAPTER_ID_PATTERN,
        )
        _require_string(self.summary, "summary", min_length=1, max_length=4096)
        _require_enum(self.kind, ActionKind, "kind")
        _require_enum(self.effect_class, EffectClass, "effect_class")
        _require_enum(self.selection_mode, SelectionMode, "selection_mode")
        _require_enum(self.preview_mode, PreviewMode, "preview_mode")
        _require_enum(self.reclaim_scope, ReclaimScope, "reclaim_scope")
        _require_enum(self.risk_tier, RiskTier, "risk_tier")
        _require_enum(self.undo_capability, UndoCapability, "undo_capability")
        _require_enum(self.reconstruction, Reconstruction, "reconstruction")
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if self.kind is ActionKind.REPORT_ONLY:
            if self.enabled:
                raise ValueError("REPORT_ONLY actions cannot be enabled")
            if (
                self.effect_class is not EffectClass.PURE_QUERY
                or self.selection_mode is not SelectionMode.NONE
                or self.preview_mode is not PreviewMode.NONE
                or self.reclaim_scope is not ReclaimScope.UNKNOWN
            ):
                raise ValueError("REPORT_ONLY actions must have non-executable semantics")

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], _jsonable(self))


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    scan_id: str
    schema_version: str
    engine_build_id: str
    created_at: datetime
    expires_at: datetime
    actions: tuple[Action, ...]

    def __post_init__(self) -> None:
        _require_string(
            self.plan_id,
            "plan_id",
            min_length=37,
            max_length=37,
            pattern=_PLAN_ID_PATTERN,
        )
        _require_string(
            self.scan_id,
            "scan_id",
            min_length=1,
            max_length=256,
            pattern=_GENERIC_ID_PATTERN,
        )
        _require_string(self.engine_build_id, "engine_build_id", min_length=1, max_length=128)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("plan schema version is unsupported")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("plan expiry must be after creation")
        if not self.actions:
            raise ValueError("plan must contain at least one action")
        if len(self.actions) > 256:
            raise ValueError("plan must contain at most 256 actions")
        if any(not isinstance(action, Action) for action in self.actions):
            raise ValueError("plan actions must be Action values")
        if any(action.kind is not ActionKind.REPORT_ONLY for action in self.actions):
            raise ValueError("the current Plan model only accepts REPORT_ONLY actions")
        action_ids = {action.action_id for action in self.actions}
        candidate_ids = {action.candidate_id for action in self.actions}
        if len(action_ids) != len(self.actions) or len(candidate_ids) != len(self.actions):
            raise ValueError("plan actions and candidates must be unique")

    @property
    def is_inventory_only(self) -> bool:
        return all(action.kind is ActionKind.REPORT_ONLY for action in self.actions)

    def to_dict(self) -> dict[str, Any]:
        payload = cast(dict[str, Any], _jsonable(self))
        payload["executable"] = False
        return payload


def new_id(prefix: str) -> str:
    """Create an opaque local identifier. It is not an authorization secret."""

    _require_string(
        prefix,
        "prefix",
        min_length=1,
        max_length=64,
        pattern=_ID_PREFIX_PATTERN,
    )
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        dataclass_values = asdict(cast(Any, value))
        return {key: _jsonable(item) for key, item in dataclass_values.items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
