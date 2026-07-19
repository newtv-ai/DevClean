"""Capability-free data contract for bounded external AI review.

This module deliberately stops at recommendations.  It cannot read, move, or
delete a file, launch a command, or turn model output into execution authority.
Candidate identifiers are packet-local opaque tokens, while snapshot digests
bind recommendations to the exact observations selected by the local caller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final, cast

from devclean.adapters.json_contract import strict_json_loads
from devclean.core.cleanup_catalog import CleanupCategory, SourceDomain
from devclean.core.triage import (
    Actionability,
    EvidenceKind,
    ExecutionPolicy,
    RecoveryCapability,
    ReviewLane,
    RiskTier,
    TriageItem,
)
from devclean.scanner.filesystem import ScanRecordKind

AI_REVIEW_SCHEMA_VERSION: Final = 1
AI_REVIEW_REQUEST_TYPE: Final = "DevClean_AI_REVIEW_REQUEST"
AI_REVIEW_RESPONSE_TYPE: Final = "DevClean_AI_REVIEW_RESPONSE"
AI_REVIEW_IMPORT_TYPE: Final = "DevClean_AI_REVIEW_RECOMMENDATIONS"
MAX_AI_REVIEW_ITEMS: Final = 100
MAX_AI_REQUEST_BYTES: Final = 512 * 1024
MAX_AI_RESPONSE_BYTES: Final = 256 * 1024
MAX_AI_REASON_CHARS: Final = 500
MAX_AI_JSON_DEPTH: Final = 32
MAX_SOURCE_REASON_CHARS: Final = 700
MAX_PATH_CHARS: Final = 32_767
MAX_TAGS_PER_ITEM: Final = 24
MAX_TAG_CHARS: Final = 64
MAX_TTL: Final = timedelta(hours=24)
DEFAULT_TTL: Final = timedelta(hours=2)

_SAFE_SCAN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SAFE_OPAQUE_ID = re.compile(r"candidate_[a-f0-9]{32}")
_SAFE_SESSION_ID = re.compile(r"review_[a-f0-9]{32}")
_SAFE_NONCE = re.compile(r"[a-f0-9]{64}")
_SAFE_DIGEST = re.compile(r"[a-f0-9]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?:^|\s)\\\\|file://|(?:^|\s)/(?:home|Users|var|tmp|etc)/|~[\\/])",
    re.IGNORECASE,
)
_RELATIVE_PATH = re.compile(r"(?:^|\s)(?:\.{1,2}[\\/]|[^\s\"']+[\\/][^\s\"']+)")
_COMMAND = re.compile(
    r"(?:"
    r"\b(?:rm|del|erase|rmdir|remove-item|powershell|pwsh|cmd(?:\.exe)?|bash|sudo)\b"
    r"|\b(?:pip|uv|npm|pnpm|conda|docker|hf)\s+(?:cache\s+)?"
    r"(?:clean|purge|remove|rm|prune)\b"
    r"|\b(?:python(?:3|\.\d+)?|node|ruby|perl)\s+"
    r"(?:-[A-Za-z]|[^\s]+\.(?:py|js|rb|pl))"
    r"|&&|\|\||\$\(|`"
    r")",
    re.IGNORECASE,
)
_PATH_SIGNAL_SEGMENTS = frozenset(
    {
        ".cache",
        ".gradle",
        "cache",
        "cacheddata",
        "caches",
        "crashdumps",
        "gradle",
        "huggingface",
        "npm-cache",
        "node_modules",
        "ollama",
        "pip",
        "pnpm",
        "pnpm-store",
        "temp",
        "tmp",
        "uv",
        "yarn",
    }
)


class AiReviewContractError(ValueError):
    """Untrusted review data violates the closed contract."""


class AiRecommendation(StrEnum):
    """The complete model vocabulary; none is an execution action."""

    KEEP = "KEEP"
    RECOMMEND_RECYCLE = "RECOMMEND_RECYCLE"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class AiReviewCandidateInput:
    """One locally selected observation offered for external review.

    ``hard_protected`` is monotonic: setting it can add protection, while
    protected triage evidence can never be downgraded by passing ``False``.
    """

    item: TriageItem
    hard_protected: bool

    def __post_init__(self) -> None:
        if not isinstance(self.item, TriageItem):
            raise TypeError("item must be a TriageItem")
        if not isinstance(self.hard_protected, bool):
            raise TypeError("hard_protected must be a bool")


@dataclass(frozen=True, slots=True)
class AiReviewEntry:
    """Private local binding between an opaque token and one scan observation."""

    candidate_id: str
    item: TriageItem = field(repr=False)
    snapshot_identity_digest: str
    hard_protected: bool
    model_metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AiReviewPackage:
    """Immutable request plus private bindings retained by DevClean."""

    review_session_id: str
    nonce: str
    scan_session_digest: str
    issued_at: datetime
    expires_at: datetime
    entries: tuple[AiReviewEntry, ...]
    package_digest: str

    def payload(self) -> dict[str, object]:
        """Return the closed, model-facing JSON document."""

        unsigned = _unsigned_request_payload(self)
        return {**unsigned, "package_digest": self.package_digest}


@dataclass(frozen=True, slots=True)
class ImportedAiRecommendation:
    """Inert model advice bound to the original local observation."""

    candidate_id: str
    item: TriageItem = field(repr=False)
    recommendation: AiRecommendation
    reason: str
    snapshot_identity_digest: str
    hard_protected: bool


@dataclass(frozen=True, slots=True)
class AiReviewImport:
    """Validated recommendations with an explicit absence of authority."""

    review_session_id: str
    package_digest: str
    recommendations: tuple[ImportedAiRecommendation, ...]
    execution_authority: str = field(default="NONE", init=False)
    document_type: str = field(default=AI_REVIEW_IMPORT_TYPE, init=False)
    schema_version: int = field(default=AI_REVIEW_SCHEMA_VERSION, init=False)


def build_ai_review_package(
    candidates: Sequence[AiReviewCandidateInput],
    *,
    scan_session_id: str,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_TTL,
) -> AiReviewPackage:
    """Build a bounded packet from explicitly supplied current-scan items."""

    selected = tuple(candidates)
    if not selected or len(selected) > MAX_AI_REVIEW_ITEMS:
        raise AiReviewContractError(
            f"AI review requires 1 to {MAX_AI_REVIEW_ITEMS} explicitly selected items"
        )
    if not isinstance(scan_session_id, str) or _SAFE_SCAN_ID.fullmatch(scan_session_id) is None:
        raise AiReviewContractError("scan_session_id must be a bounded opaque identifier")
    if not isinstance(ttl, timedelta) or ttl <= timedelta(0) or ttl > MAX_TTL:
        raise AiReviewContractError("AI review ttl must be positive and at most 24 hours")

    issued_at = _aware_utc(now or datetime.now(UTC), "now")
    expires_at = issued_at + ttl
    nonce = secrets.token_hex(32)
    review_session_id = f"review_{secrets.token_hex(16)}"
    scan_session_digest = _digest_text(scan_session_id)
    entries: list[AiReviewEntry] = []
    normalized_paths: set[str] = set()
    for source in selected:
        if not isinstance(source, AiReviewCandidateInput):
            raise TypeError("candidates must contain AiReviewCandidateInput values")
        item = source.item
        _validate_triage_item(item)
        normalized_path = _normalized_path(item.path)
        if normalized_path in normalized_paths:
            raise AiReviewContractError("AI review candidates contain a duplicate path")
        normalized_paths.add(normalized_path)
        hard_protected = source.hard_protected or _triage_is_hard_protected(item)
        candidate_id = f"candidate_{secrets.token_hex(16)}"
        snapshot_digest = _snapshot_digest(item, nonce)
        entries.append(
            AiReviewEntry(
                candidate_id=candidate_id,
                item=item,
                snapshot_identity_digest=snapshot_digest,
                hard_protected=hard_protected,
                model_metadata=_model_metadata(item, hard_protected, snapshot_digest),
            )
        )

    provisional = AiReviewPackage(
        review_session_id=review_session_id,
        nonce=nonce,
        scan_session_digest=scan_session_digest,
        issued_at=issued_at,
        expires_at=expires_at,
        entries=tuple(entries),
        package_digest="0" * 64,
    )
    digest = _digest_json(_unsigned_request_payload(provisional))
    package = AiReviewPackage(
        review_session_id=review_session_id,
        nonce=nonce,
        scan_session_digest=scan_session_digest,
        issued_at=issued_at,
        expires_at=expires_at,
        entries=tuple(entries),
        package_digest=digest,
    )
    serialized = serialize_ai_review_package(package)
    if len(serialized.encode("utf-8")) > MAX_AI_REQUEST_BYTES:
        raise AiReviewContractError("AI review request exceeds the byte limit")
    return package


def serialize_ai_review_package(package: AiReviewPackage) -> str:
    """Serialize one internally valid request; this performs no file I/O."""

    _validate_package_object(package, now=None, check_expiry=False)
    return json.dumps(
        package.payload(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_ai_review_package_text(
    text: str,
    package: AiReviewPackage,
    *,
    now: datetime | None = None,
) -> None:
    """Fail closed if an exported request was changed or has expired."""

    payload = _strict_bounded_json(text, MAX_AI_REQUEST_BYTES, "AI review request")
    _validate_package_object(package, now=now, check_expiry=True)
    if not _json_exact_equal(payload, package.payload()):
        raise AiReviewContractError("AI review request differs from the local package")
    if not isinstance(payload, dict):
        raise AiReviewContractError("AI review request must be a JSON object")
    supplied_digest = payload.get("package_digest")
    unsigned = {key: value for key, value in payload.items() if key != "package_digest"}
    if not isinstance(supplied_digest, str) or not hmac.compare_digest(
        supplied_digest, _digest_json(unsigned)
    ):
        raise AiReviewContractError("AI review request digest mismatch")


def parse_ai_review_response(
    text: str,
    package: AiReviewPackage,
    *,
    now: datetime | None = None,
) -> AiReviewImport:
    """Import complete model advice without creating an executable action."""

    payload = _strict_bounded_json(text, MAX_AI_RESPONSE_BYTES, "AI review response")
    _validate_package_object(package, now=now, check_expiry=True)
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "document_type",
        "review_session_id",
        "nonce",
        "package_digest",
        "recommendations",
    }:
        raise AiReviewContractError("AI response has unknown or missing top-level fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise AiReviewContractError("AI response has an unsupported schema version")
    if payload["document_type"] != AI_REVIEW_RESPONSE_TYPE:
        raise AiReviewContractError("AI response has the wrong document type")
    for binding_field, expected_value in (
        ("review_session_id", package.review_session_id),
        ("nonce", package.nonce),
        ("package_digest", package.package_digest),
    ):
        value = payload[binding_field]
        if not isinstance(value, str) or not hmac.compare_digest(value, expected_value):
            raise AiReviewContractError(f"AI response {binding_field} mismatch")

    raw_recommendations = payload["recommendations"]
    if not isinstance(raw_recommendations, list):
        raise AiReviewContractError("AI response recommendations must be an array")
    if len(raw_recommendations) != len(package.entries):
        raise AiReviewContractError("AI response must cover every candidate exactly once")
    expected_entries = {entry.candidate_id: entry for entry in package.entries}
    imported: dict[str, ImportedAiRecommendation] = {}
    for raw in raw_recommendations:
        if not isinstance(raw, dict) or set(raw) != {
            "candidate_id",
            "recommendation",
            "reason",
        }:
            raise AiReviewContractError("AI recommendation has unknown or missing fields")
        candidate_id = raw["candidate_id"]
        if (
            not isinstance(candidate_id, str)
            or candidate_id not in expected_entries
            or candidate_id in imported
        ):
            raise AiReviewContractError("AI response contains an unknown or duplicate candidate")
        recommendation_value = raw["recommendation"]
        if not isinstance(recommendation_value, str):
            raise AiReviewContractError("AI recommendation token must be a string")
        try:
            recommendation = AiRecommendation(recommendation_value)
        except ValueError as error:
            raise AiReviewContractError("AI response has an unknown recommendation") from error
        reason = _validate_model_reason(raw["reason"])
        entry = expected_entries[candidate_id]
        if entry.hard_protected and recommendation is AiRecommendation.RECOMMEND_RECYCLE:
            raise AiReviewContractError("AI cannot recommend recycling a hard-protected item")
        imported[candidate_id] = ImportedAiRecommendation(
            candidate_id=candidate_id,
            item=entry.item,
            recommendation=recommendation,
            reason=reason,
            snapshot_identity_digest=entry.snapshot_identity_digest,
            hard_protected=entry.hard_protected,
        )

    if set(imported) != set(expected_entries):
        raise AiReviewContractError("AI response must cover every candidate exactly once")
    return AiReviewImport(
        review_session_id=package.review_session_id,
        package_digest=package.package_digest,
        recommendations=tuple(imported[entry.candidate_id] for entry in package.entries),
    )


def response_template(package: AiReviewPackage) -> dict[str, object]:
    """Return a closed inert response skeleton for a model or human reviewer."""

    _validate_package_object(package, now=None, check_expiry=False)
    return {
        "schema_version": AI_REVIEW_SCHEMA_VERSION,
        "document_type": AI_REVIEW_RESPONSE_TYPE,
        "review_session_id": package.review_session_id,
        "nonce": package.nonce,
        "package_digest": package.package_digest,
        "recommendations": [
            {
                "candidate_id": entry.candidate_id,
                "recommendation": AiRecommendation.UNSURE.value,
                "reason": "Insufficient metadata; local human review is required.",
            }
            for entry in package.entries
        ],
    }


def _unsigned_request_payload(package: AiReviewPackage) -> dict[str, object]:
    return {
        "schema_version": AI_REVIEW_SCHEMA_VERSION,
        "document_type": AI_REVIEW_REQUEST_TYPE,
        "execution_authority": "NONE",
        "review_session_id": package.review_session_id,
        "nonce": package.nonce,
        "scan_session_digest": package.scan_session_digest,
        "issued_at": _format_utc(package.issued_at),
        "expires_at": _format_utc(package.expires_at),
        "instructions": [
            "Use only the supplied metadata; do not infer or emit paths or commands.",
            "Return KEEP, RECOMMEND_RECYCLE, or UNSURE for every candidate exactly once.",
            "RECOMMEND_RECYCLE is advice only and never grants execution authority.",
            "Hard-protected candidates must be KEEP or UNSURE.",
        ],
        "response_contract": {
            "schema_version": AI_REVIEW_SCHEMA_VERSION,
            "document_type": AI_REVIEW_RESPONSE_TYPE,
            "closed_fields": True,
            "recommendation_enum": [member.value for member in AiRecommendation],
            "reason_max_characters": MAX_AI_REASON_CHARS,
            "paths_or_commands_permitted": False,
        },
        "candidates": [
            {"candidate_id": entry.candidate_id, **dict(entry.model_metadata)}
            for entry in package.entries
        ],
    }


def _model_metadata(
    item: TriageItem, hard_protected: bool, snapshot_digest: str
) -> Mapping[str, object]:
    reason = _bounded_source_text(item.reason, MAX_SOURCE_REASON_CHARS, "reason")
    tags = []
    for tag in item.tags[:MAX_TAGS_PER_ITEM]:
        bounded = _bounded_source_text(tag, MAX_TAG_CHARS, "tag")
        if bounded not in tags:
            tags.append(bounded)
    return {
        "redacted_path_hint": _redacted_path_hint(item.path),
        "source_domain": item.source_domain.value,
        "category": item.category.value,
        "review_lane": item.lane.value,
        "risk_tier": item.risk_tier.value,
        "evidence_kind": item.evidence_kind.value,
        "execution_policy": item.execution_policy.value,
        "recovery_capability": item.recovery.value,
        "logical_size_bytes": item.logical_size,
        "allocated_size_bytes": item.allocated_size,
        "last_write_time_ns": item.record.last_write_time_ns,
        "reason": reason,
        "tags": tags,
        "hard_protected": hard_protected,
        "snapshot_identity_digest": snapshot_digest,
    }


def _snapshot_digest(item: TriageItem, nonce: str) -> str:
    snapshot = {
        "normalized_path": _normalized_path(item.path),
        "root": _normalized_path(item.record.root),
        "kind": item.record.kind.value,
        "logical_size": item.logical_size,
        "allocated_size": item.allocated_size,
        "raw_allocated_size": item.record.raw_allocated_size,
        "volume_serial": item.record.volume_serial,
        "file_id": item.record.file_id,
        "file_id_kind": item.record.file_id_kind,
        "link_count": item.record.link_count,
        "attributes": item.record.attributes,
        "reparse_tag": item.record.reparse_tag,
        "creation_time_ns": item.record.creation_time_ns,
        "last_write_time_ns": item.record.last_write_time_ns,
        "hardlink_duplicate": item.record.hardlink_duplicate,
        "allocation_uncertain": item.record.allocation_uncertain,
        "category": item.category.value,
        "lane": item.lane.value,
        "risk_tier": item.risk_tier.value,
        "evidence_kind": item.evidence_kind.value,
        "actionability": item.actionability.value,
        "execution_policy": item.execution_policy.value,
        "recovery": item.recovery.value,
    }
    return hmac.new(
        bytes.fromhex(nonce),
        _canonical_json(snapshot),
        hashlib.sha256,
    ).hexdigest()


def _triage_is_hard_protected(item: TriageItem) -> bool:
    return (
        item.lane is ReviewLane.PROTECTED
        or item.risk_tier is RiskTier.PROTECTED
        or item.actionability is Actionability.PROTECTED
    )


def _normalized_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS:
        raise AiReviewContractError("candidate path is empty or exceeds the path limit")
    if _CONTROL.search(value) is not None:
        raise AiReviewContractError("candidate path contains control characters")
    return os.path.normcase(os.path.normpath(os.path.abspath(value)))


def _redacted_path_hint(value: str) -> str:
    """Expose structural cache signals and suffix, never an absolute path or basename."""

    windows = "\\" in value or re.match(r"^[A-Za-z]:", value) is not None
    pure = PureWindowsPath(value) if windows else PurePosixPath(value)
    signals: list[str] = []
    for part in pure.parts:
        folded = part.casefold()
        if folded in _PATH_SIGNAL_SEGMENTS and folded not in signals:
            signals.append(folded)
    suffix = pure.suffix.casefold()
    safe_suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) else "<none>"
    signal_text = "/".join(signals[-3:]) if signals else "<none>"
    return f"signals={signal_text}; filename=*{safe_suffix}"


def _validate_model_reason(value: object) -> str:
    if not isinstance(value, str):
        raise AiReviewContractError("AI recommendation reason must be a string")
    reason = value.strip()
    if not reason or len(reason) > MAX_AI_REASON_CHARS:
        raise AiReviewContractError(
            f"AI recommendation reason must be 1 to {MAX_AI_REASON_CHARS} characters"
        )
    if _CONTROL.search(reason) is not None:
        raise AiReviewContractError("AI recommendation reason contains control characters")
    if _ABSOLUTE_PATH.search(reason) is not None or _RELATIVE_PATH.search(reason) is not None:
        raise AiReviewContractError("AI recommendation reason must not contain a path")
    if _COMMAND.search(reason) is not None:
        raise AiReviewContractError("AI recommendation reason must not contain a command")
    try:
        reason.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise AiReviewContractError("AI recommendation reason is not valid Unicode") from error
    return reason


def _bounded_source_text(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str):
        raise AiReviewContractError(f"candidate {label} must be text")
    cleaned = _CONTROL.sub(" ", value).strip()
    if not cleaned:
        return "<not supplied>"
    if _ABSOLUTE_PATH.search(cleaned) is not None or _RELATIVE_PATH.search(cleaned) is not None:
        return "<redacted path-bearing source metadata>"
    if _COMMAND.search(cleaned) is not None:
        return "<redacted command-like source metadata>"
    if len(cleaned) > limit:
        return cleaned[: limit - 1] + "…"
    return cleaned


def _validate_package_object(
    package: AiReviewPackage,
    *,
    now: datetime | None,
    check_expiry: bool,
) -> None:
    if not isinstance(package, AiReviewPackage):
        raise TypeError("package must be an AiReviewPackage")
    if _SAFE_SESSION_ID.fullmatch(package.review_session_id) is None:
        raise AiReviewContractError("local review session id is invalid")
    if _SAFE_NONCE.fullmatch(package.nonce) is None:
        raise AiReviewContractError("local review nonce is invalid")
    if _SAFE_DIGEST.fullmatch(package.scan_session_digest) is None:
        raise AiReviewContractError("local scan session digest is invalid")
    if _SAFE_DIGEST.fullmatch(package.package_digest) is None:
        raise AiReviewContractError("local package digest is invalid")
    issued_at = _aware_utc(package.issued_at, "issued_at")
    expires_at = _aware_utc(package.expires_at, "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > MAX_TTL:
        raise AiReviewContractError("local package has an invalid validity window")
    if not package.entries or len(package.entries) > MAX_AI_REVIEW_ITEMS:
        raise AiReviewContractError("local package has an invalid candidate count")
    ids: set[str] = set()
    paths: set[str] = set()
    for entry in package.entries:
        if _SAFE_OPAQUE_ID.fullmatch(entry.candidate_id) is None or entry.candidate_id in ids:
            raise AiReviewContractError("local package has an invalid candidate id")
        ids.add(entry.candidate_id)
        _validate_triage_item(entry.item)
        path = _normalized_path(entry.item.path)
        if path in paths:
            raise AiReviewContractError("local package has duplicate candidate paths")
        paths.add(path)
        if _SAFE_DIGEST.fullmatch(entry.snapshot_identity_digest) is None:
            raise AiReviewContractError("local package has an invalid snapshot digest")
        expected_snapshot = _snapshot_digest(entry.item, package.nonce)
        if not hmac.compare_digest(entry.snapshot_identity_digest, expected_snapshot):
            raise AiReviewContractError("local candidate snapshot digest mismatch")
        if _triage_is_hard_protected(entry.item) and not entry.hard_protected:
            raise AiReviewContractError("local package downgraded a hard-protected candidate")
        expected_metadata = _model_metadata(
            entry.item, entry.hard_protected, entry.snapshot_identity_digest
        )
        if not _json_exact_equal(dict(entry.model_metadata), dict(expected_metadata)):
            raise AiReviewContractError("local candidate metadata mismatch")
    expected_digest = _digest_json(_unsigned_request_payload(package))
    if not hmac.compare_digest(package.package_digest, expected_digest):
        raise AiReviewContractError("local package digest mismatch")
    if check_expiry:
        current = _aware_utc(now or datetime.now(UTC), "now")
        if current < issued_at - timedelta(minutes=5):
            raise AiReviewContractError("AI review package is not yet valid")
        if current > expires_at:
            raise AiReviewContractError("AI review package has expired")


def _validate_triage_item(item: TriageItem) -> None:
    if not isinstance(item, TriageItem):
        raise TypeError("review candidate item must be a TriageItem")
    if item.record.kind is not ScanRecordKind.FILE:
        raise AiReviewContractError("AI review accepts file observations only")
    if not os.path.isabs(item.path) or not os.path.isabs(item.record.root):
        raise AiReviewContractError("AI review candidate paths must be absolute")
    normalized_path = _normalized_path(item.path)
    normalized_record_path = _normalized_path(item.record.path)
    if normalized_path != normalized_record_path:
        raise AiReviewContractError("triage and scan record paths do not match")
    normalized_root = _normalized_path(item.record.root)
    try:
        contained = os.path.commonpath((normalized_root, normalized_path)) == normalized_root
    except ValueError as error:
        raise AiReviewContractError(
            "candidate path is on a different volume from its scan root"
        ) from error
    if not contained:
        raise AiReviewContractError("candidate path escapes its scan root")
    if item.logical_size != item.record.logical_size:
        raise AiReviewContractError("triage and scan logical sizes do not match")
    if item.allocated_size != item.record.allocated_size:
        raise AiReviewContractError("triage and scan allocated sizes do not match")
    _bounded_integer(item.logical_size, "logical_size", maximum=(1 << 63) - 1)
    _optional_bounded_integer(
        item.allocated_size, "allocated_size", maximum=(1 << 63) - 1
    )
    _optional_bounded_integer(
        item.record.raw_allocated_size,
        "raw_allocated_size",
        maximum=(1 << 63) - 1,
    )
    _bounded_integer(item.record.depth, "depth", maximum=1_000_000)
    _optional_bounded_integer(
        item.record.volume_serial, "volume_serial", maximum=(1 << 64) - 1
    )
    _optional_bounded_integer(item.record.link_count, "link_count", maximum=(1 << 32) - 1)
    _optional_bounded_integer(item.record.attributes, "attributes", maximum=(1 << 32) - 1)
    _optional_bounded_integer(item.record.reparse_tag, "reparse_tag", maximum=(1 << 32) - 1)
    for timestamp_label, timestamp_value in (
        ("creation_time_ns", item.record.creation_time_ns),
        ("last_write_time_ns", item.record.last_write_time_ns),
    ):
        _optional_bounded_integer(
            timestamp_value, timestamp_label, maximum=(1 << 63) - 1
        )
    for text_label, text_value, text_limit in (
        ("file_id", item.record.file_id, 256),
        ("file_id_kind", item.record.file_id_kind, 64),
    ):
        if text_value is not None and (
            not isinstance(text_value, str)
            or not text_value
            or len(text_value) > text_limit
            or _CONTROL.search(text_value) is not None
        ):
            raise AiReviewContractError(f"candidate {text_label} is invalid")
    enum_fields: tuple[tuple[object, type[StrEnum], str], ...] = (
        (item.category, CleanupCategory, "category"),
        (item.source_domain, SourceDomain, "source_domain"),
        (item.lane, ReviewLane, "review_lane"),
        (item.risk_tier, RiskTier, "risk_tier"),
        (item.evidence_kind, EvidenceKind, "evidence_kind"),
        (item.actionability, Actionability, "actionability"),
        (item.execution_policy, ExecutionPolicy, "execution_policy"),
        (item.recovery, RecoveryCapability, "recovery_capability"),
    )
    for enum_value, enum_type, enum_label in enum_fields:
        if not isinstance(enum_value, enum_type):
            raise AiReviewContractError(
                f"candidate {enum_label} is not a known enum value"
            )


def _bounded_integer(value: object, label: str, *, maximum: int) -> None:
    if type(value) is not int or value < 0 or value > maximum:
        raise AiReviewContractError(f"candidate {label} is outside the supported range")


def _optional_bounded_integer(value: object, label: str, *, maximum: int) -> None:
    if value is not None:
        _bounded_integer(value, label, maximum=maximum)


def _strict_bounded_json(text: str, max_bytes: int, label: str) -> object:
    if not isinstance(text, str):
        raise TypeError(f"{label} must be UTF-8 JSON text")
    if text.startswith("\ufeff"):
        raise AiReviewContractError(f"{label} must not contain a UTF-8 BOM")
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise AiReviewContractError(f"{label} is not valid Unicode") from error
    if size < 2 or size > max_bytes:
        raise AiReviewContractError(f"{label} exceeds the byte limit")
    _reject_excessive_json_nesting(text, label)
    try:
        return strict_json_loads(text)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
        raise AiReviewContractError(f"{label} is not strict JSON: {error}") from error


def _reject_excessive_json_nesting(text: str, label: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_AI_JSON_DEPTH:
                raise AiReviewContractError(f"{label} exceeds the JSON nesting limit")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                break


def _format_utc(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AiReviewContractError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise AiReviewContractError("review data is not canonical JSON") from error


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _json_exact_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        typed_right = cast(dict[object, object], right)
        if set(left) != set(typed_right):
            return False
        return all(_json_exact_equal(value, typed_right[key]) for key, value in left.items())
    if isinstance(left, list):
        typed_list = cast(list[object], right)
        return len(left) == len(typed_list) and all(
            _json_exact_equal(item, typed_list[index]) for index, item in enumerate(left)
        )
    return left == right


__all__ = [
    "AI_REVIEW_IMPORT_TYPE",
    "AI_REVIEW_REQUEST_TYPE",
    "AI_REVIEW_RESPONSE_TYPE",
    "AI_REVIEW_SCHEMA_VERSION",
    "DEFAULT_TTL",
    "MAX_AI_JSON_DEPTH",
    "MAX_AI_REASON_CHARS",
    "MAX_AI_REQUEST_BYTES",
    "MAX_AI_RESPONSE_BYTES",
    "MAX_AI_REVIEW_ITEMS",
    "AiRecommendation",
    "AiReviewCandidateInput",
    "AiReviewContractError",
    "AiReviewEntry",
    "AiReviewImport",
    "AiReviewPackage",
    "ImportedAiRecommendation",
    "build_ai_review_package",
    "parse_ai_review_response",
    "response_template",
    "serialize_ai_review_package",
    "validate_ai_review_package_text",
]
