"""Fail-closed contracts for future incremental inventory scans.

The v0.1 contract deliberately supports only a live
``ReadDirectoryChangesW`` session as a *shadow* change source.  A valid
checkpoint may reduce classification work in a future release, but this
module never authorizes directory pruning, file deletion, elevation, or USN
journal access.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Self
from uuid import uuid4

INCREMENTAL_CONTRACT_VERSION = "1.0.0"

_SESSION_TOKEN = re.compile(r"^session_[a-f0-9]{32}$")
_GENERATION_ID = re.compile(r"^generation_[a-f0-9]{32}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class ScanMode(StrEnum):
    """How a generation was requested or observed.

    ``SESSION_SHADOW`` records change hints while the ordinary scanner still
    performs its complete traversal.  It must not be interpreted as authority
    to omit any directory.
    """

    FULL_BASELINE = "FULL_BASELINE"
    FULL_RECONCILIATION = "FULL_RECONCILIATION"
    SESSION_SHADOW = "SESSION_SHADOW"


class ChangeSource(StrEnum):
    """Bounded change sources supported by the current contract."""

    NONE = "NONE"
    READ_DIRECTORY_CHANGES_W_SESSION = "READ_DIRECTORY_CHANGES_W_SESSION"


class ScanCoverage(StrEnum):
    """Whether the observational result covered its approved roots."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"


class FallbackReason(StrEnum):
    """Reasons that invalidate a session checkpoint and require full scan."""

    NO_BASELINE = "NO_BASELINE"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    MONITOR_NOT_READY = "MONITOR_NOT_READY"
    MONITOR_STOPPED = "MONITOR_STOPPED"
    MONITOR_STOP_TIMEOUT = "MONITOR_STOP_TIMEOUT"
    MONITOR_BUFFER_OVERFLOW = "MONITOR_BUFFER_OVERFLOW"
    MONITOR_CAPACITY_EXCEEDED = "MONITOR_CAPACITY_EXCEEDED"
    MONITOR_HANDLE_LOST = "MONITOR_HANDLE_LOST"
    MONITOR_IO_ERROR = "MONITOR_IO_ERROR"
    MALFORMED_NOTIFICATION = "MALFORMED_NOTIFICATION"
    ROOT_CHANGED = "ROOT_CHANGED"
    ROOT_IDENTITY_UNAVAILABLE = "ROOT_IDENTITY_UNAVAILABLE"
    SESSION_TOKEN_MISMATCH = "SESSION_TOKEN_MISMATCH"
    SESSION_SEQUENCE_GAP = "SESSION_SEQUENCE_GAP"
    SESSION_SEQUENCE_REGRESSION = "SESSION_SEQUENCE_REGRESSION"
    SCOPE_CHANGED = "SCOPE_CHANGED"
    CHECKPOINT_INCOMPATIBLE = "CHECKPOINT_INCOMPATIBLE"


def new_generation_id() -> str:
    """Return a bounded opaque generation identifier."""

    return f"generation_{uuid4().hex}"


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_json_object(raw: str) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > 1_048_576:
        raise ValueError("incremental contract JSON must be bounded text")
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except (TypeError, ValueError) as error:
        raise ValueError("incremental contract is not strict JSON") from error
    if not isinstance(value, dict):
        raise ValueError("incremental contract must be a JSON object")
    return value


def _require_closed_keys(
    value: Mapping[str, object], expected: frozenset[str], field_name: str
) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{field_name} has unexpected or missing fields")


def _require_pattern(value: object, pattern: re.Pattern[str], field_name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _require_non_negative_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _parse_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError(f"{field_name} must be a bounded timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} is not an ISO timestamp") from error
    return _require_aware_datetime(parsed, field_name)


def _parse_enum(value: object, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    try:
        return enum_type(value)
    except ValueError as error:
        raise ValueError(f"{field_name} is unsupported") from error


@dataclass(frozen=True, slots=True)
class SessionCheckpoint:
    """A cursor that is valid only for one live monitor session."""

    contract_version: str
    session_token: str
    scope_digest: str
    last_sequence: int
    created_at: datetime
    source: ChangeSource = ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION
    reusable_across_processes: bool = False

    def __post_init__(self) -> None:
        if self.contract_version != INCREMENTAL_CONTRACT_VERSION:
            raise ValueError("unsupported incremental checkpoint contract version")
        _require_pattern(self.session_token, _SESSION_TOKEN, "session_token")
        _require_pattern(self.scope_digest, _SHA256, "scope_digest")
        _require_non_negative_integer(self.last_sequence, "last_sequence")
        _require_aware_datetime(self.created_at, "created_at")
        if not isinstance(self.source, ChangeSource):
            raise ValueError("session checkpoint source must be a ChangeSource")
        if self.source is not ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION:
            raise ValueError("session checkpoints require ReadDirectoryChangesW")
        if self.reusable_across_processes is not False:
            raise ValueError("session checkpoints can never be reusable across processes")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "session_token": self.session_token,
            "scope_digest": self.scope_digest,
            "last_sequence": self.last_sequence,
            "created_at": self.created_at.isoformat(),
            "source": self.source.value,
            "reusable_across_processes": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        expected = frozenset(
            {
                "contract_version",
                "session_token",
                "scope_digest",
                "last_sequence",
                "created_at",
                "source",
                "reusable_across_processes",
            }
        )
        _require_closed_keys(value, expected, "session checkpoint")
        reusable = value["reusable_across_processes"]
        if reusable is not False:
            raise ValueError("session checkpoint persistence flag must be false")
        return cls(
            contract_version=_require_pattern(
                value["contract_version"],
                re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$"),
                "contract_version",
            ),
            session_token=_require_pattern(value["session_token"], _SESSION_TOKEN, "session_token"),
            scope_digest=_require_pattern(value["scope_digest"], _SHA256, "scope_digest"),
            last_sequence=_require_non_negative_integer(value["last_sequence"], "last_sequence"),
            created_at=_parse_datetime(value["created_at"], "created_at"),
            source=ChangeSource(_parse_enum(value["source"], ChangeSource, "source")),
            reusable_across_processes=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> Self:
        return cls.from_dict(_load_json_object(raw))


@dataclass(frozen=True, slots=True)
class IncrementalDecision:
    """Fail-closed decision for one requested session-shadow scan."""

    requested_mode: ScanMode
    effective_mode: ScanMode
    change_source: ChangeSource
    checkpoint_usable: bool
    fallback_reason: FallbackReason | None
    may_skip_directories: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requested_mode, ScanMode) or not isinstance(
            self.effective_mode, ScanMode
        ):
            raise ValueError("incremental decision modes must be ScanMode values")
        if not isinstance(self.change_source, ChangeSource):
            raise ValueError("incremental decision source must be a ChangeSource")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason, FallbackReason
        ):
            raise ValueError("incremental fallback must be a FallbackReason")
        if not isinstance(self.checkpoint_usable, bool):
            raise ValueError("checkpoint_usable must be a boolean")
        if self.may_skip_directories is not False:
            raise ValueError("v0.1 incremental decisions cannot skip directories")
        if self.effective_mode is ScanMode.SESSION_SHADOW:
            if (
                self.requested_mode is not ScanMode.SESSION_SHADOW
                or self.change_source is not ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION
                or not self.checkpoint_usable
                or self.fallback_reason is not None
            ):
                raise ValueError("usable session-shadow decision is inconsistent")
        elif self.checkpoint_usable:
            raise ValueError("a full-scan decision cannot mark its checkpoint usable")
        if (
            self.requested_mode is ScanMode.SESSION_SHADOW
            and self.effective_mode is not ScanMode.SESSION_SHADOW
            and self.fallback_reason is None
        ):
            raise ValueError("session-shadow fallback must record a reason")

    @property
    def requires_full_scan(self) -> bool:
        return self.effective_mode is not ScanMode.SESSION_SHADOW


def evaluate_session_checkpoint(
    checkpoint: SessionCheckpoint | None,
    *,
    live_session_token: str,
    scope_digest: str,
    next_sequence: int,
    monitor_fallback: FallbackReason | None = None,
) -> IncrementalDecision:
    """Evaluate a live cursor without ever enabling directory pruning."""

    _require_non_negative_integer(next_sequence, "next_sequence")
    if monitor_fallback is not None:
        return _full_scan_decision(monitor_fallback)
    if checkpoint is None:
        return _full_scan_decision(FallbackReason.NO_BASELINE)
    if _SESSION_TOKEN.fullmatch(live_session_token) is None:
        return _full_scan_decision(FallbackReason.SESSION_TOKEN_MISMATCH)
    if _SHA256.fullmatch(scope_digest) is None or checkpoint.scope_digest != scope_digest:
        return _full_scan_decision(FallbackReason.SCOPE_CHANGED)
    if checkpoint.session_token != live_session_token:
        return _full_scan_decision(FallbackReason.SESSION_TOKEN_MISMATCH)
    if checkpoint.last_sequence > next_sequence:
        return _full_scan_decision(FallbackReason.SESSION_SEQUENCE_REGRESSION)
    return IncrementalDecision(
        requested_mode=ScanMode.SESSION_SHADOW,
        effective_mode=ScanMode.SESSION_SHADOW,
        change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
        checkpoint_usable=True,
        fallback_reason=None,
    )


def _full_scan_decision(reason: FallbackReason) -> IncrementalDecision:
    return IncrementalDecision(
        requested_mode=ScanMode.SESSION_SHADOW,
        effective_mode=ScanMode.FULL_RECONCILIATION,
        change_source=ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION,
        checkpoint_usable=False,
        fallback_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class ScanGeneration:
    """Immutable audit metadata for one observational scan generation."""

    contract_version: str
    generation_id: str
    scope_digest: str
    requested_mode: ScanMode
    actual_mode: ScanMode
    change_source: ChangeSource
    coverage: ScanCoverage
    started_at: datetime
    finished_at: datetime
    parent_generation_id: str | None = None
    checkpoint: SessionCheckpoint | None = None
    fallback_reason: FallbackReason | None = None
    events_observed: int = 0
    paths_invalidated: int = 0
    directories_reobserved: int = 0
    entries_reused: int = 0
    used_for_directory_pruning: bool = False

    def __post_init__(self) -> None:
        if self.contract_version != INCREMENTAL_CONTRACT_VERSION:
            raise ValueError("unsupported scan generation contract version")
        _require_pattern(self.generation_id, _GENERATION_ID, "generation_id")
        _require_pattern(self.scope_digest, _SHA256, "scope_digest")
        if not isinstance(self.requested_mode, ScanMode) or not isinstance(
            self.actual_mode, ScanMode
        ):
            raise ValueError("generation modes must be ScanMode values")
        if not isinstance(self.change_source, ChangeSource):
            raise ValueError("generation source must be a ChangeSource")
        if not isinstance(self.coverage, ScanCoverage):
            raise ValueError("generation coverage must be a ScanCoverage value")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason, FallbackReason
        ):
            raise ValueError("generation fallback must be a FallbackReason")
        if self.checkpoint is not None and not isinstance(self.checkpoint, SessionCheckpoint):
            raise ValueError("generation checkpoint must be a SessionCheckpoint")
        if self.parent_generation_id is not None:
            _require_pattern(self.parent_generation_id, _GENERATION_ID, "parent_generation_id")
        _require_aware_datetime(self.started_at, "started_at")
        _require_aware_datetime(self.finished_at, "finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        for name in (
            "events_observed",
            "paths_invalidated",
            "directories_reobserved",
            "entries_reused",
        ):
            _require_non_negative_integer(getattr(self, name), name)
        if self.used_for_directory_pruning is not False:
            raise ValueError("v0.1 generations cannot prune directory traversal")
        if self.checkpoint is not None and self.checkpoint.scope_digest != self.scope_digest:
            raise ValueError("generation checkpoint scope does not match")
        if self.actual_mode is ScanMode.SESSION_SHADOW and (
            self.requested_mode is not ScanMode.SESSION_SHADOW
            or self.change_source is not ChangeSource.READ_DIRECTORY_CHANGES_W_SESSION
            or self.checkpoint is None
            or self.fallback_reason is not None
        ):
            raise ValueError("session-shadow generation is inconsistent")
        if self.fallback_reason is not None and (
            self.requested_mode is not ScanMode.SESSION_SHADOW
            or self.actual_mode is not ScanMode.FULL_RECONCILIATION
        ):
            raise ValueError("fallback reason requires a full reconciliation")
        if (
            self.requested_mode is ScanMode.SESSION_SHADOW
            and self.actual_mode is not ScanMode.SESSION_SHADOW
            and self.fallback_reason is None
        ):
            raise ValueError("session fallback generation must record its reason")
        if self.change_source is ChangeSource.NONE and (
            self.checkpoint is not None or self.events_observed != 0
        ):
            raise ValueError("a generation without a change source cannot have change data")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "generation_id": self.generation_id,
            "scope_digest": self.scope_digest,
            "requested_mode": self.requested_mode.value,
            "actual_mode": self.actual_mode.value,
            "change_source": self.change_source.value,
            "coverage": self.coverage.value,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "parent_generation_id": self.parent_generation_id,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "fallback_reason": (self.fallback_reason.value if self.fallback_reason else None),
            "events_observed": self.events_observed,
            "paths_invalidated": self.paths_invalidated,
            "directories_reobserved": self.directories_reobserved,
            "entries_reused": self.entries_reused,
            "used_for_directory_pruning": False,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Self:
        expected = frozenset(
            {
                "contract_version",
                "generation_id",
                "scope_digest",
                "requested_mode",
                "actual_mode",
                "change_source",
                "coverage",
                "started_at",
                "finished_at",
                "parent_generation_id",
                "checkpoint",
                "fallback_reason",
                "events_observed",
                "paths_invalidated",
                "directories_reobserved",
                "entries_reused",
                "used_for_directory_pruning",
            }
        )
        _require_closed_keys(value, expected, "scan generation")
        parent = value["parent_generation_id"]
        if parent is not None:
            parent = _require_pattern(parent, _GENERATION_ID, "parent_generation_id")
        checkpoint_raw = value["checkpoint"]
        if checkpoint_raw is None:
            checkpoint = None
        elif isinstance(checkpoint_raw, Mapping):
            checkpoint = SessionCheckpoint.from_dict(checkpoint_raw)
        else:
            raise ValueError("checkpoint must be an object or null")
        fallback_raw = value["fallback_reason"]
        fallback = (
            None
            if fallback_raw is None
            else FallbackReason(_parse_enum(fallback_raw, FallbackReason, "fallback_reason"))
        )
        if value["used_for_directory_pruning"] is not False:
            raise ValueError("directory pruning flag must be false")
        return cls(
            contract_version=_require_pattern(
                value["contract_version"],
                re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$"),
                "contract_version",
            ),
            generation_id=_require_pattern(value["generation_id"], _GENERATION_ID, "generation_id"),
            scope_digest=_require_pattern(value["scope_digest"], _SHA256, "scope_digest"),
            requested_mode=ScanMode(
                _parse_enum(value["requested_mode"], ScanMode, "requested_mode")
            ),
            actual_mode=ScanMode(_parse_enum(value["actual_mode"], ScanMode, "actual_mode")),
            change_source=ChangeSource(
                _parse_enum(value["change_source"], ChangeSource, "change_source")
            ),
            coverage=ScanCoverage(_parse_enum(value["coverage"], ScanCoverage, "coverage")),
            started_at=_parse_datetime(value["started_at"], "started_at"),
            finished_at=_parse_datetime(value["finished_at"], "finished_at"),
            parent_generation_id=parent,
            checkpoint=checkpoint,
            fallback_reason=fallback,
            events_observed=_require_non_negative_integer(
                value["events_observed"], "events_observed"
            ),
            paths_invalidated=_require_non_negative_integer(
                value["paths_invalidated"], "paths_invalidated"
            ),
            directories_reobserved=_require_non_negative_integer(
                value["directories_reobserved"], "directories_reobserved"
            ),
            entries_reused=_require_non_negative_integer(value["entries_reused"], "entries_reused"),
            used_for_directory_pruning=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> Self:
        return cls.from_dict(_load_json_object(raw))


__all__ = [
    "INCREMENTAL_CONTRACT_VERSION",
    "ChangeSource",
    "FallbackReason",
    "IncrementalDecision",
    "ScanCoverage",
    "ScanGeneration",
    "ScanMode",
    "SessionCheckpoint",
    "evaluate_session_checkpoint",
    "new_generation_id",
]
