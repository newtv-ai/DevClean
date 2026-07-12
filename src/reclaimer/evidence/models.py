"""Versioned evidence metadata for bounded external observations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, TypeAlias

from reclaimer.core.models import EffectClass
from reclaimer.evidence.redaction import TRANSCRIPT_REDACTION_VERSION, TranscriptStorage

_SHA256_LENGTH = 64
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_ADAPTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE_ERROR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 11434
LOOPBACK_METHOD = "GET"
LOOPBACK_RESPONSE_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        "/api/version": 64 * 1024,
        "/api/tags": 16 * 1024 * 1024,
        "/api/ps": 2 * 1024 * 1024,
    }
)
LOOPBACK_ENDPOINTS = frozenset(LOOPBACK_RESPONSE_LIMITS)


class EvidenceKind(StrEnum):
    VENDOR_CLI = "VENDOR_CLI"
    LOOPBACK_API = "LOOPBACK_API"
    FILESYSTEM_METADATA = "FILESYSTEM_METADATA"


class LoopbackOutcome(StrEnum):
    RESPONSE = "RESPONSE"
    FAILURE = "FAILURE"


class LoopbackContentTypeClass(StrEnum):
    APPLICATION_JSON = "APPLICATION_JSON"
    OTHER = "OTHER"
    MISSING = "MISSING"


class LoopbackContentEncodingClass(StrEnum):
    IDENTITY = "IDENTITY"
    OTHER = "OTHER"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class CommandEvidence:
    evidence_id: str
    scan_id: str
    adapter_id: str
    kind: EvidenceKind
    captured_at: datetime
    executable_path: str
    executable_size: int
    executable_mtime_ns: int
    executable_volume_serial: str | None
    executable_file_id: str | None
    executable_file_id_kind: str | None
    executable_sha256: str
    argv_redacted: tuple[str, ...]
    effect_class: EffectClass
    returncode: int | None
    duration_ms: int
    timed_out: bool
    output_limit_exceeded: bool
    transcript_redaction_version: str
    stdout_size: int
    stderr_size: int
    stdout_sha256: str
    stderr_sha256: str
    stdout_stored_size: int
    stderr_stored_size: int
    stdout_stored_sha256: str
    stderr_stored_sha256: str
    stdout_storage: TranscriptStorage
    stderr_storage: TranscriptStorage
    stdout_file: str
    stderr_file: str

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.evidence_id) or not _SAFE_ID.fullmatch(
            self.scan_id
        ):
            raise ValueError("command evidence IDs are not safe")
        if not _SAFE_ADAPTER_ID.fullmatch(self.adapter_id):
            raise ValueError("command evidence adapter_id is not safe")
        if self.kind is not EvidenceKind.VENDOR_CLI:
            raise ValueError("CommandEvidence kind must be VENDOR_CLI")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        _validate_non_negative_integers(
            (
                self.executable_size,
                self.executable_mtime_ns,
                self.duration_ms,
                self.stdout_size,
                self.stderr_size,
                self.stdout_stored_size,
                self.stderr_stored_size,
            ),
            "command evidence numeric fields",
        )
        if (
            not isinstance(self.executable_path, str)
            or not 1 <= len(self.executable_path) <= 32_767
            or not isinstance(self.argv_redacted, tuple)
            or not self.argv_redacted
            or len(self.argv_redacted) > 128
        ):
            raise ValueError("command evidence executable and argv are required")
        if any(
            not isinstance(argument, str) or not 1 <= len(argument) <= 32_767
            for argument in self.argv_redacted
        ):
            raise ValueError("command evidence argv metadata is invalid")
        if self.effect_class not in {
            EffectClass.PURE_QUERY,
            EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        }:
            raise ValueError("command evidence effect class is invalid")
        if not isinstance(self.timed_out, bool) or not isinstance(
            self.output_limit_exceeded, bool
        ):
            raise ValueError("command evidence failure flags must be booleans")
        if isinstance(self.returncode, bool) or (
            self.returncode is not None and not isinstance(self.returncode, int)
        ):
            raise ValueError("command evidence returncode is invalid")
        if not isinstance(self.stdout_storage, TranscriptStorage) or not isinstance(
            self.stderr_storage, TranscriptStorage
        ):
            raise ValueError("command transcript storage classification is invalid")
        if self.transcript_redaction_version != TRANSCRIPT_REDACTION_VERSION:
            raise ValueError("unsupported transcript_redaction_version")
        hashes = (
            self.executable_sha256,
            self.stdout_sha256,
            self.stderr_sha256,
            self.stdout_stored_sha256,
            self.stderr_stored_sha256,
        )
        _validate_hashes(hashes)
        identity = (
            self.executable_volume_serial,
            self.executable_file_id,
            self.executable_file_id_kind,
        )
        if any(value is None for value in identity) and any(
            value is not None for value in identity
        ):
            raise ValueError("executable identity fields must be all present or all absent")
        if self.executable_volume_serial is not None and (
            not re.fullmatch(r"[a-f0-9]{1,16}", self.executable_volume_serial)
            or self.executable_file_id is None
            or not re.fullmatch(r"[a-f0-9]{1,32}", self.executable_file_id)
            or self.executable_file_id_kind is None
            or not re.fullmatch(r"[a-z0-9_]{1,32}", self.executable_file_id_kind)
        ):
            raise ValueError("executable identity fields are invalid")
        if self.stdout_file != (
            f"commands/{self.evidence_id}.stdout.redacted.txt"
        ) or self.stderr_file != f"commands/{self.evidence_id}.stderr.redacted.txt":
            raise ValueError("command transcript paths must be scan-local redacted files")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["captured_at"] = self.captured_at.isoformat()
        payload["effect_class"] = self.effect_class.value
        payload["argv_redacted"] = list(self.argv_redacted)
        payload["stdout_storage"] = self.stdout_storage.value
        payload["stderr_storage"] = self.stderr_storage.value
        return payload


@dataclass(frozen=True, slots=True)
class LoopbackEvidence:
    """Auditable evidence for one fixed, read-only Ollama loopback GET.

    The model intentionally cannot represent a free-form URL, hostname, port,
    method, or endpoint. ``response_sha256`` identifies the original bounded
    bytes observed in memory; only the separately identified redacted/marker
    representation is allowed to reach ``response_file``.
    """

    evidence_id: str
    scan_id: str
    adapter_id: str
    kind: EvidenceKind
    captured_at: datetime
    host: str
    port: int
    method: str
    endpoint: str
    effect_class: EffectClass
    outcome: LoopbackOutcome
    http_status: int | None
    error_type: str | None
    duration_ms: int
    timed_out: bool
    output_limit_exceeded: bool
    content_type_class: LoopbackContentTypeClass
    content_encoding_class: LoopbackContentEncodingClass
    transcript_redaction_version: str
    response_size: int
    response_sha256: str
    response_stored_size: int
    response_stored_sha256: str
    response_storage: TranscriptStorage
    response_file: str

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.evidence_id):
            raise ValueError("evidence_id is not safe")
        if not _SAFE_ID.fullmatch(self.scan_id):
            raise ValueError("scan_id is not safe")
        if self.adapter_id != "ollama":
            raise ValueError("LoopbackEvidence adapter_id must be ollama")
        if self.kind is not EvidenceKind.LOOPBACK_API:
            raise ValueError("LoopbackEvidence kind must be LOOPBACK_API")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.host != LOOPBACK_HOST or self.port != LOOPBACK_PORT:
            raise ValueError("LoopbackEvidence must use the fixed Ollama loopback address")
        if self.method != LOOPBACK_METHOD or self.endpoint not in LOOPBACK_ENDPOINTS:
            raise ValueError("LoopbackEvidence method or endpoint is not allowlisted")
        if self.effect_class is not EffectClass.PURE_QUERY:
            raise ValueError("LoopbackEvidence must be a PURE_QUERY")
        _validate_non_negative_integers(
            (
                self.port,
                self.duration_ms,
                self.response_size,
                self.response_stored_size,
            ),
            "loopback evidence numeric fields",
        )
        if self.transcript_redaction_version != TRANSCRIPT_REDACTION_VERSION:
            raise ValueError("unsupported transcript_redaction_version")
        _validate_hashes((self.response_sha256, self.response_stored_sha256))
        if self.response_file != (
            f"loopback/{self.evidence_id}.response.redacted.txt"
        ):
            raise ValueError("response_file must be the scan-local redacted transcript path")
        if not isinstance(self.content_type_class, LoopbackContentTypeClass):
            raise ValueError("content_type_class must be a closed classification")
        if not isinstance(self.content_encoding_class, LoopbackContentEncodingClass):
            raise ValueError("content_encoding_class must be a closed classification")
        if not isinstance(self.response_storage, TranscriptStorage):
            raise ValueError("response_storage must be a closed classification")
        if not isinstance(self.timed_out, bool) or not isinstance(
            self.output_limit_exceeded, bool
        ):
            raise ValueError("loopback failure flags must be booleans")

        if self.outcome is LoopbackOutcome.RESPONSE:
            if (
                isinstance(self.http_status, bool)
                or not isinstance(self.http_status, int)
                or not 100 <= self.http_status <= 599
            ):
                raise ValueError("response evidence requires an HTTP status")
            if self.error_type is not None:
                raise ValueError("response evidence cannot have error_type")
            if self.timed_out or self.output_limit_exceeded:
                raise ValueError("completed response cannot have failure flags")
        elif self.outcome is LoopbackOutcome.FAILURE:
            if self.http_status is not None:
                raise ValueError("connection failure cannot have an HTTP status")
            if self.error_type is None or not _SAFE_ERROR_TYPE.fullmatch(self.error_type):
                raise ValueError("connection failure requires a safe error_type")
        else:
            raise ValueError("unsupported loopback outcome")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["captured_at"] = self.captured_at.isoformat()
        payload["effect_class"] = self.effect_class.value
        payload["outcome"] = self.outcome.value
        payload["content_type_class"] = self.content_type_class.value
        payload["content_encoding_class"] = self.content_encoding_class.value
        payload["response_storage"] = self.response_storage.value
        return payload


EvidenceRecord: TypeAlias = CommandEvidence | LoopbackEvidence


def _validate_hashes(hashes: tuple[str, ...]) -> None:
    if any(
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
        for value in hashes
    ):
        raise ValueError("evidence SHA-256 fields must be 64 lowercase hex characters")


def _validate_non_negative_integers(values: tuple[object, ...], field_name: str) -> None:
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in values
    ):
        raise ValueError(f"{field_name} must be non-negative integers")


__all__ = [
    "LOOPBACK_ENDPOINTS",
    "LOOPBACK_HOST",
    "LOOPBACK_METHOD",
    "LOOPBACK_PORT",
    "LOOPBACK_RESPONSE_LIMITS",
    "CommandEvidence",
    "EvidenceKind",
    "EvidenceRecord",
    "LoopbackContentEncodingClass",
    "LoopbackContentTypeClass",
    "LoopbackEvidence",
    "LoopbackOutcome",
    "TranscriptStorage",
]
