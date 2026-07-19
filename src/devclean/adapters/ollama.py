"""Read-only Ollama inventory through the fixed local loopback API.

This adapter deliberately has no CLI path.  It never starts, stops, loads, pulls,
creates, or removes a model, and it never follows HTTP redirects.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from devclean.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from devclean.core.models import (
    Confidence,
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    SizeValue,
    new_id,
)
from devclean.evidence.models import (
    LOOPBACK_ENDPOINTS,
    LOOPBACK_HOST,
    LOOPBACK_METHOD,
    LOOPBACK_PORT,
    LOOPBACK_RESPONSE_LIMITS,
    LoopbackEvidence,
)

_HTTP_TIMEOUT_SECONDS = 5.0
_ENDPOINT_LIMITS = LOOPBACK_RESPONSE_LIMITS
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_MAX_RECORDS = 100_000
_MAX_INTEGER = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class LoopbackResponse:
    status: int
    content_type: str | None
    content_encoding: str | None
    body: bytes
    duration_ms: int


class LoopbackRequestError(Exception):
    """A bounded request failure whose safe attributes may enter evidence metadata."""

    def __init__(
        self,
        error_type: str,
        *,
        duration_ms: int,
        timed_out: bool = False,
        output_limit_exceeded: bool = False,
        body: bytes = b"",
        unavailable: bool = False,
    ) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.duration_ms = duration_ms
        self.timed_out = timed_out
        self.output_limit_exceeded = output_limit_exceeded
        self.body = body
        self.unavailable = unavailable


@dataclass(frozen=True, slots=True)
class OllamaModel:
    name: str
    model: str
    modified_at: str
    size: int
    digest: str
    format: str
    family: str
    families: tuple[str, ...]
    parameter_size: str
    quantization_level: str


@dataclass(frozen=True, slots=True)
class OllamaRunningModel:
    name: str
    model: str
    size: int
    digest: str
    expires_at: str
    size_vram: int
    context_length: int


LoopbackGetter = Callable[[str], LoopbackResponse]


class OllamaAdapter:
    id = "ollama"

    def __init__(self, http_get: LoopbackGetter | None = None) -> None:
        self._http_get = http_get or get_loopback

    def inventory(self, context: AdapterContext) -> InventoryResult:
        records: list[LoopbackEvidence] = []
        observations: list[Evidence] = []
        try:
            try:
                version_response, version_evidence = _fetch_and_record(
                    context, self._http_get, "/api/version", records
                )
            except (OSError, http.client.HTTPException, LoopbackRequestError) as error:
                unavailable = isinstance(error, OSError) or (
                    isinstance(error, LoopbackRequestError) and error.unavailable
                )
                if not unavailable:
                    raise
                return InventoryResult(
                    self.id,
                    ProbeResult(
                        self.id,
                        ProbeStatus.UNAVAILABLE,
                        detail=(
                            "The fixed Ollama loopback endpoint is unavailable; DevClean did "
                            "not start the service."
                        ),
                    ),
                    issues=(
                        AdapterIssue(
                            "SERVICE_UNAVAILABLE",
                            "Ollama was not started and inventory was skipped.",
                        ),
                    ),
                    evidence=tuple(records),
                )

            observations.append(version_evidence)
            version = parse_version_response(
                _validate_response("/api/version", version_response)
            )

            tags_response, tags_evidence = _fetch_and_record(
                context, self._http_get, "/api/tags", records
            )
            observations.append(tags_evidence)
            models = parse_tags_response(_validate_response("/api/tags", tags_response))

            ps_response, ps_evidence = _fetch_and_record(
                context, self._http_get, "/api/ps", records
            )
            observations.append(ps_evidence)
            running = parse_ps_response(_validate_response("/api/ps", ps_response))

            resources, issues = _resources_from_inventory(
                models,
                running,
                evidence=(version_evidence, tags_evidence, ps_evidence),
            )
            return InventoryResult(
                self.id,
                ProbeResult(
                    self.id,
                    ProbeStatus.AVAILABLE,
                    version=version,
                    detail="fixed 127.0.0.1 loopback API read-only inventory verified",
                ),
                resources=resources,
                issues=issues,
                evidence=tuple(records),
            )
        except (
            OSError,
            http.client.HTTPException,
            UnicodeError,
            ValueError,
        ) as error:
            evidence_suffix = (
                ""
                if not records
                else f" Last local evidence: evidence:{records[-1].evidence_id}."
            )
            message = (
                f"Ollama inventory failed closed: {type(error).__name__}."
                f"{evidence_suffix}"
            )
            return InventoryResult(
                self.id,
                ProbeResult(
                    self.id,
                    ProbeStatus.ERROR,
                    detail=message,
                ),
                issues=(AdapterIssue("ADAPTER_ERROR", message, True),),
                evidence=tuple(records),
            )


def get_loopback(endpoint: str) -> LoopbackResponse:
    """GET one allowlisted endpoint directly, without proxy or redirect support."""

    if endpoint not in LOOPBACK_ENDPOINTS or endpoint not in _ENDPOINT_LIMITS:
        raise ValueError("Ollama endpoint is not allowlisted")
    limit = _ENDPOINT_LIMITS[endpoint]
    started = time.perf_counter_ns()
    connection = http.client.HTTPConnection(
        LOOPBACK_HOST,
        LOOPBACK_PORT,
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    try:
        connection.request(
            LOOPBACK_METHOD,
            endpoint,
            body=None,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "DevClean-inventory/0.1.1",
            },
        )
        response = connection.getresponse()
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            normalized = content_length.strip()
            if not normalized.isascii() or not normalized.isdecimal():
                raise LoopbackRequestError(
                    "InvalidContentLength",
                    duration_ms=_elapsed_ms(started),
                )
            if int(normalized) > limit:
                raise LoopbackRequestError(
                    "ResponseLimitExceeded",
                    duration_ms=_elapsed_ms(started),
                    output_limit_exceeded=True,
                )
        body = response.read(limit + 1)
        if len(body) > limit:
            raise LoopbackRequestError(
                "ResponseLimitExceeded",
                duration_ms=_elapsed_ms(started),
                output_limit_exceeded=True,
                body=body,
            )
        return LoopbackResponse(
            status=response.status,
            content_type=response.getheader("Content-Type"),
            content_encoding=response.getheader("Content-Encoding"),
            body=body,
            duration_ms=_elapsed_ms(started),
        )
    except LoopbackRequestError:
        raise
    except TimeoutError as error:
        raise LoopbackRequestError(
            type(error).__name__,
            duration_ms=_elapsed_ms(started),
            timed_out=True,
            unavailable=True,
        ) from error
    except OSError as error:
        raise LoopbackRequestError(
            type(error).__name__,
            duration_ms=_elapsed_ms(started),
            unavailable=True,
        ) from error
    except http.client.HTTPException as error:
        partial = getattr(error, "partial", b"")
        raise LoopbackRequestError(
            type(error).__name__,
            duration_ms=_elapsed_ms(started),
            body=partial if isinstance(partial, bytes) else b"",
        ) from error
    finally:
        connection.close()


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def parse_version_response(body: bytes) -> str:
    payload = _json_object(body, "Ollama version")
    version = _safe_text(payload.get("version"), "version")
    if not _VERSION.fullmatch(version):
        raise ValueError("Ollama version has an unsupported format")
    return version


def parse_tags_response(body: bytes) -> tuple[OllamaModel, ...]:
    payload = _json_object(body, "Ollama tags")
    records = payload.get("models")
    if not isinstance(records, list) or len(records) > _MAX_RECORDS:
        raise ValueError("Ollama tags models must be a bounded array")
    models: list[OllamaModel] = []
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Ollama tags model must be an object")
        name = _safe_text(item.get("name"), "name")
        model = _safe_text(item.get("model"), "model")
        if model in seen:
            raise ValueError("Ollama tags contains a duplicate model")
        seen.add(model)
        modified_at = _timestamp(item.get("modified_at"), "modified_at")
        size = _nonnegative_int(item.get("size"), "size")
        digest = _digest(item.get("digest"))
        details = _details(item.get("details"), allow_parent=False)
        models.append(
            OllamaModel(
                name=name,
                model=model,
                modified_at=modified_at,
                size=size,
                digest=digest,
                format=details[0],
                family=details[1],
                families=details[2],
                parameter_size=details[3],
                quantization_level=details[4],
            )
        )
    return tuple(models)


def parse_ps_response(body: bytes) -> tuple[OllamaRunningModel, ...]:
    payload = _json_object(body, "Ollama ps")
    records = payload.get("models")
    if not isinstance(records, list) or len(records) > _MAX_RECORDS:
        raise ValueError("Ollama ps models must be a bounded array")
    running: list[OllamaRunningModel] = []
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Ollama running model must be an object")
        digest = _digest(item.get("digest"))
        if digest in seen:
            raise ValueError("Ollama ps contains a duplicate digest")
        seen.add(digest)
        _details(item.get("details"), allow_parent=True)
        running.append(
            OllamaRunningModel(
                name=_safe_text(item.get("name"), "name"),
                model=_safe_text(item.get("model"), "model"),
                size=_nonnegative_int(item.get("size"), "size"),
                digest=digest,
                expires_at=_timestamp(item.get("expires_at"), "expires_at"),
                size_vram=_nonnegative_int(item.get("size_vram"), "size_vram"),
                context_length=_nonnegative_int(
                    item.get("context_length"), "context_length"
                ),
            )
        )
    return tuple(running)


def _resources_from_inventory(
    models: tuple[OllamaModel, ...],
    running: tuple[OllamaRunningModel, ...],
    *,
    evidence: tuple[Evidence, ...],
) -> tuple[tuple[Resource, ...], tuple[AdapterIssue, ...]]:
    resources: list[Resource] = []
    matched_running: set[str] = set()
    issues: list[AdapterIssue] = []
    for model in models:
        active = next(
            (
                item
                for item in running
                if item.digest == model.digest
                or item.model in {model.model, model.name}
                or item.name in {model.model, model.name}
            ),
            None,
        )
        warnings = [
            "Ollama's integer size is treated as vendor logical/manifest metadata, not "
            "exclusive host bytes.",
            "Models may share blobs, so model sizes must not be summed as physical "
            "reclaimable space.",
            "A local, custom, private, or deleted upstream model may not be reconstructible.",
            "No Ollama CLI, service control, model load, pull, create, stop, or remove was "
            "performed.",
        ]
        risk = RiskTier.YELLOW
        if active is not None:
            matched_running.add(active.digest)
            risk = RiskTier.RED
            warnings.append(
                "This model is currently loaded according to /api/ps; in-use models are "
                "high-risk and its RAM/VRAM values are not disk usage."
            )
            warnings.append(
                f"Vendor runtime observation: size_vram={active.size_vram}, "
                f"context_length={active.context_length}, expires_at={active.expires_at}."
            )
            if active.digest != model.digest:
                issues.append(
                    AdapterIssue(
                        "RUNNING_DIGEST_MISMATCH",
                        "A running model name matched an installed tag but its digest differed.",
                    )
                )
        resources.append(
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="ollama",
                display_name=f"Ollama model {model.name}",
                semantic_type=SemanticType.INSTALLED_MODEL,
                risk_tier=risk,
                provenance_class=ProvenanceClass.UNKNOWN,
                vendor_locator=f"ollama:{model.model}@sha256:{model.digest.lower()}",
                logical_size=SizeValue(model.size, Confidence.ESTIMATE),
                reconstruction=Reconstruction.NONE,
                warnings=tuple(warnings),
                evidence=evidence,
                actionable=False,
            )
        )

    unmatched = [item for item in running if item.digest not in matched_running]
    if unmatched:
        issues.append(
            AdapterIssue(
                "RUNNING_MODEL_NOT_LISTED",
                f"{len(unmatched)} running model record(s) did not match /api/tags; no disk "
                "candidate was inferred from runtime state alone.",
            )
        )
    return (tuple(resources), tuple(issues))


def _fetch_and_record(
    context: AdapterContext,
    getter: LoopbackGetter,
    endpoint: str,
    records: list[LoopbackEvidence],
) -> tuple[LoopbackResponse, Evidence]:
    try:
        response = getter(endpoint)
    except (OSError, http.client.HTTPException, LoopbackRequestError) as error:
        if isinstance(error, LoopbackRequestError):
            error_type = error.error_type
            duration_ms = error.duration_ms
            timed_out = error.timed_out
            output_limit_exceeded = error.output_limit_exceeded
            body = error.body
        else:
            error_type = type(error).__name__
            duration_ms = 0
            timed_out = isinstance(error, TimeoutError)
            output_limit_exceeded = False
            body = b""
        record = context.record_loopback_failure(
            adapter_id="ollama",
            endpoint=endpoint,
            error_type=error_type,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_limit_exceeded=output_limit_exceeded,
            body=body,
        )
        records.append(record)
        raise

    record = context.record_loopback_response(
        adapter_id="ollama",
        endpoint=endpoint,
        status=response.status,
        duration_ms=response.duration_ms,
        body=response.body,
        content_type=response.content_type,
        content_encoding=response.content_encoding,
    )
    records.append(record)
    return (response, _core_evidence(record))


def _validate_response(endpoint: str, response: LoopbackResponse) -> bytes:
    limit = _ENDPOINT_LIMITS.get(endpoint)
    if limit is None or endpoint not in LOOPBACK_ENDPOINTS:
        raise ValueError("Ollama endpoint is not allowlisted")
    if (
        isinstance(response.status, bool)
        or not isinstance(response.status, int)
        or not 100 <= response.status <= 599
    ):
        raise ValueError("Ollama HTTP status is invalid")
    if not isinstance(response.body, bytes) or len(response.body) > limit:
        raise ValueError("Ollama response exceeds its endpoint limit")
    if (
        isinstance(response.duration_ms, bool)
        or not isinstance(response.duration_ms, int)
        or response.duration_ms < 0
    ):
        raise ValueError("Ollama response duration is invalid")
    content_type = response.content_type
    if content_type is not None and not _is_safe_text(content_type, allow_empty=False):
        raise ValueError("Ollama Content-Type is unsafe")
    content_encoding = response.content_encoding
    if content_encoding is not None and not _is_safe_text(
        content_encoding, allow_empty=False
    ):
        raise ValueError("Ollama Content-Encoding is unsafe")

    if response.status != 200:
        raise ValueError("Ollama endpoint did not return HTTP 200")
    if content_type is None or content_type.split(";", 1)[0].strip().lower() != (
        "application/json"
    ):
        raise ValueError("Ollama endpoint did not return application/json")
    if content_encoding is not None and content_encoding.strip().lower() != "identity":
        raise ValueError("Ollama compressed responses are not accepted")
    return response.body


def _core_evidence(record: LoopbackEvidence) -> Evidence:
    return Evidence(
        source=f"evidence:{record.evidence_id}",
        detail=(
            f"GET {LOOPBACK_HOST}:{LOOPBACK_PORT}{record.endpoint} bounded loopback response"
        ),
        checked_at=record.captured_at,
        digest=record.response_sha256,
    )


def _json_object(body: bytes, label: str) -> dict[str, Any]:
    if not isinstance(body, bytes):
        raise ValueError(f"{label} response must be bytes")
    try:
        text = body.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} response is not strict JSON") from error
    if not isinstance(payload, dict) or len(payload) > 64:
        raise ValueError(f"{label} response must be a bounded object")
    return payload


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ollama JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Ollama JSON constant {value!r} is not permitted")


def _details(
    value: Any, *, allow_parent: bool
) -> tuple[str, str, tuple[str, ...], str, str]:
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("Ollama model details must be a bounded object")
    if allow_parent:
        _safe_text(value.get("parent_model"), "parent_model", allow_empty=True)
    format_value = _safe_text(value.get("format"), "format")
    family = _safe_text(value.get("family"), "family")
    families_value = value.get("families")
    if not isinstance(families_value, list) or len(families_value) > 128:
        raise ValueError("Ollama model families must be a bounded array")
    families = tuple(_safe_text(item, "families") for item in families_value)
    if len(set(families)) != len(families):
        raise ValueError("Ollama model families contains duplicates")
    parameter_size = _safe_text(value.get("parameter_size"), "parameter_size")
    quantization = _safe_text(
        value.get("quantization_level"), "quantization_level"
    )
    return (format_value, family, families, parameter_size, quantization)


def _digest(value: Any) -> str:
    digest = _safe_text(value, "digest")
    if not _DIGEST.fullmatch(digest):
        raise ValueError("Ollama digest must be 64 hexadecimal characters")
    return digest.lower()


def _timestamp(value: Any, field: str) -> str:
    timestamp = _safe_text(value, field)
    if not _TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"Ollama {field} must be an ISO 8601 timestamp")
    return timestamp


def _nonnegative_int(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > _MAX_INTEGER
    ):
        raise ValueError(f"Ollama {field} must be a bounded non-negative integer")
    return value


def _safe_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > 32_767
        or not _is_safe_text(value, allow_empty=allow_empty)
    ):
        raise ValueError(f"Ollama {field} is not safe text")
    return value


def _is_safe_text(value: str, *, allow_empty: bool) -> bool:
    return (allow_empty or bool(value)) and not any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
    )


__all__ = [
    "LoopbackRequestError",
    "LoopbackResponse",
    "OllamaAdapter",
    "OllamaModel",
    "OllamaRunningModel",
    "get_loopback",
    "parse_ps_response",
    "parse_tags_response",
    "parse_version_response",
]
