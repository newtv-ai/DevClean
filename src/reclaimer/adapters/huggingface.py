"""Hugging Face cache inventory with versioned JSON-shape validation."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from reclaimer.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from reclaimer.adapters.command import QueryCommand, decode_utf8
from reclaimer.adapters.discovery import (
    VersionTuple,
    format_version,
    parse_version,
    resolve_executable,
)
from reclaimer.adapters.json_contract import strict_json_loads
from reclaimer.core.models import (
    Confidence,
    EffectClass,
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    SizeValue,
    new_id,
)
from reclaimer.evidence.models import CommandEvidence
from reclaimer.platform.windows.process import ProcessLimits
from reclaimer.platform.windows.volumes import is_local_fixed_path

_REVISION = re.compile(r"^[0-9a-fA-F]{40}$")
_HUMAN_SIZE = re.compile(r"^([0-9]+(?:\.[0-9])?)([KMGTPE]?)$")
_SIZE_FACTORS = {
    "": 1,
    "K": 1_000,
    "M": 1_000_000,
    "G": 1_000_000_000,
    "T": 1_000_000_000_000,
    "P": 1_000_000_000_000_000,
    "E": 1_000_000_000_000_000_000,
}
_SUPPORTED_REPO_TYPES = frozenset({"model", "dataset", "space"})
_PROBE_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=256 * 1024,
    max_stderr_bytes=256 * 1024,
)
_INVENTORY_LIMITS = ProcessLimits(
    timeout_seconds=300,
    max_stdout_bytes=32 * 1024 * 1024,
    max_stderr_bytes=2 * 1024 * 1024,
)
_ENVIRONMENT = (
    ("HF_HUB_OFFLINE", "1"),
    ("HF_HUB_DISABLE_UPDATE_CHECK", "1"),
    ("HF_HUB_DISABLE_TELEMETRY", "1"),
    ("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1"),
    ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
)


class HuggingFaceAdapter:
    id = "huggingface"

    def __init__(
        self,
        executable: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.environment = dict(os.environ if environment is None else environment)

    def inventory(self, context: AdapterContext) -> InventoryResult:
        executable = self.executable or resolve_executable("hf")
        if (
            executable is None
            or not executable.is_absolute()
            or not executable.is_file()
            or not is_local_fixed_path(executable)
        ):
            probe = ProbeResult(self.id, ProbeStatus.UNAVAILABLE, detail="hf.exe not found")
            return InventoryResult(self.id, probe)

        evidence = []
        try:
            version_observation = context.observe(
                QueryCommand(
                    self.id,
                    executable,
                    ("--version",),
                    EffectClass.PURE_QUERY,
                    _PROBE_LIMITS,
                    _ENVIRONMENT,
                )
            )
            evidence.append(version_observation.evidence)
            if not version_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "VERSION_QUERY_FAILED",
                    "hf --version failed, timed out, or exceeded its output limit.",
                )
            version = parse_version(decode_utf8(version_observation.result))
            if version is None:
                return _failure(
                    executable,
                    evidence,
                    "UNKNOWN_VERSION",
                    "hf returned an unrecognized version string.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                )
            version_text = format_version(version)
            if version < (1, 0, 0) or version >= (2, 0, 0):
                probe = ProbeResult(
                    self.id,
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                    str(executable),
                    "hf cache inventory supports huggingface_hub versions >=1 and <2.",
                )
                return InventoryResult(self.id, probe, evidence=tuple(evidence))

            help_observation = context.observe(
                QueryCommand(
                    self.id,
                    executable,
                    ("cache", "ls", "--help"),
                    EffectClass.PURE_QUERY,
                    _PROBE_LIMITS,
                    _ENVIRONMENT,
                )
            )
            evidence.append(help_observation.evidence)
            help_text = decode_utf8(help_observation.result)
            if not help_observation.result.succeeded or not all(
                option in help_text for option in ("--revisions", "--format", "--cache-dir")
            ):
                return _failure(
                    executable,
                    evidence,
                    "CAPABILITY_MISSING",
                    "hf cache ls does not expose the required read-only options.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )

            cache_root = discover_hub_root(self.environment)
            if cache_root is None:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_ROOT_UNSAFE",
                    "The configured Hugging Face cache root is not an absolute fixed local path.",
                    ProbeStatus.ERROR,
                    version_text,
                )
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(executable),
                "read-only cache CLI verified",
            )
            if not cache_root.exists():
                return InventoryResult(
                    self.id,
                    probe,
                    issues=(
                        AdapterIssue(
                            "CACHE_ROOT_MISSING",
                            "Hugging Face cache root does not exist; inventory is empty.",
                        ),
                    ),
                    evidence=tuple(evidence),
                )

            inventory_observation = context.observe(
                QueryCommand(
                    self.id,
                    executable,
                    (
                        "cache",
                        "ls",
                        "--revisions",
                        "--format",
                        "json",
                        "--cache-dir",
                        str(cache_root),
                    ),
                    EffectClass.PURE_QUERY,
                    _INVENTORY_LIMITS,
                    _ENVIRONMENT,
                )
            )
            evidence.append(inventory_observation.evidence)
            if not inventory_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "INVENTORY_QUERY_FAILED",
                    "hf cache ls failed, timed out, or exceeded its output limit.",
                    ProbeStatus.ERROR,
                    version_text,
                )
            resources = parse_cache_inventory(
                decode_utf8(inventory_observation.result),
                cache_root=cache_root,
                version=version,
                evidence=Evidence(
                    source=f"evidence:{inventory_observation.evidence.evidence_id}",
                    detail="hf cache ls --revisions structured output",
                    checked_at=inventory_observation.evidence.captured_at,
                    digest=inventory_observation.evidence.stdout_sha256,
                ),
            )
            return InventoryResult(
                self.id,
                probe,
                resources=resources,
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                executable,
                evidence,
                "ADAPTER_ERROR",
                f"Hugging Face inventory failed closed: {type(error).__name__}.",
            )


def discover_hub_root(environment: Mapping[str, str]) -> Path | None:
    if environment.get("HF_HUB_CACHE"):
        root = Path(environment["HF_HUB_CACHE"])
    elif environment.get("HF_HOME"):
        root = Path(environment["HF_HOME"]) / "hub"
    elif environment.get("XDG_CACHE_HOME"):
        root = Path(environment["XDG_CACHE_HOME"]) / "huggingface" / "hub"
    elif environment.get("USERPROFILE"):
        root = Path(environment["USERPROFILE"]) / ".cache" / "huggingface" / "hub"
    else:
        return None
    if not root.is_absolute() or str(root).startswith((r"\\", "//")):
        return None
    return root if is_local_fixed_path(root) else None


def parse_cache_inventory(
    text: str,
    *,
    cache_root: Path,
    version: VersionTuple,
    evidence: Evidence | None = None,
) -> tuple[Resource, ...]:
    payload = strict_json_loads(text)
    if not isinstance(payload, list):
        raise ValueError("hf cache JSON root must be an array")
    if len(payload) > 100_000:
        raise ValueError("hf cache JSON contains too many records")
    resources: list[Resource] = []
    seen: set[tuple[str, str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("hf cache record must be an object")
        resource, key = _parse_record(
            item,
            cache_root,
            evidence,
            expected_shape=_shape_for_version(version),
        )
        if key in seen:
            raise ValueError("hf cache JSON contains a duplicate revision")
        seen.add(key)
        resources.append(resource)
    return tuple(resources)


def _parse_record(
    item: dict[str, Any],
    cache_root: Path,
    evidence: Evidence | None,
    *,
    expected_shape: Literal["A", "B"],
) -> tuple[Resource, tuple[str, str, str]]:
    repo_id = _safe_text(item.get("repo_id"), "repo_id")
    repo_type = _safe_text(item.get("repo_type"), "repo_type")
    if repo_type not in _SUPPORTED_REPO_TYPES:
        raise ValueError("hf cache repo_type is unsupported")
    revision = _safe_text(item.get("revision"), "revision")
    if not _REVISION.fullmatch(revision):
        raise ValueError("hf cache revision must be a full 40-character hash")
    snapshot = _contained_snapshot(item.get("snapshot_path"), cache_root)
    refs = item.get("refs")
    if not isinstance(refs, list) or not all(
        isinstance(ref, str) and _is_safe_text(ref) for ref in refs
    ):
        raise ValueError("hf cache refs must be a safe string array")

    if expected_shape == "A" and "size_on_disk" in item and "size" not in item:
        size_value = item["size_on_disk"]
        if isinstance(size_value, bool) or not isinstance(size_value, int) or size_value < 0:
            raise ValueError("hf legacy size_on_disk must be a non-negative integer")
        for timestamp_field in ("last_accessed", "last_modified"):
            timestamp = item.get(timestamp_field)
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise ValueError(f"hf legacy {timestamp_field} must be an epoch number")
        logical_size = SizeValue(size_value, Confidence.EXACT)
        shape_warning = "Vendor revision size is exact logical data, not exclusive host bytes."
    elif expected_shape == "B" and "size" in item and "size_on_disk" not in item:
        _safe_text(item.get("id"), "id")
        logical_size = SizeValue(_parse_human_size(item["size"]), Confidence.ESTIMATE)
        if not isinstance(item.get("last_modified"), str):
            raise ValueError("hf current last_modified must be display text")
        shape_warning = (
            "Vendor revision size was rounded to one decimal place and is only an estimate."
        )
    else:
        raise ValueError("hf cache record does not match the shape bound to this version")

    semantic_type = (
        SemanticType.INSTALLED_MODEL
        if repo_type == "model"
        else SemanticType.PACKAGE_STORE
    )
    locator = f"{repo_type}:{repo_id}@{revision.lower()}"
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="huggingface",
        display_name=f"Hugging Face {repo_type} revision",
        semantic_type=semantic_type,
        risk_tier=RiskTier.YELLOW,
        provenance_class=ProvenanceClass.UNKNOWN,
        vendor_locator=locator,
        path=str(snapshot),
        logical_size=logical_size,
        reconstruction=Reconstruction.REDOWNLOAD_BEST_EFFORT,
        reconstruction_preconditions=(
            "Remote object and exact revision still exist.",
            "Required credentials and network access remain available.",
        ),
        warnings=(
            shape_warning,
            "Revisions may share blobs; sizes must not be summed as exclusive reclaimable bytes.",
            "Offline inventory cannot verify that a private or deleted revision is retrievable.",
        ),
        evidence=(() if evidence is None else (evidence,)),
        actionable=False,
    )
    return resource, (repo_type, repo_id, revision.lower())


def _shape_for_version(version: VersionTuple) -> Literal["A", "B"]:
    if (1, 0, 0) <= version < (1, 11, 0):
        return "A"
    if (1, 11, 0) <= version < (2, 0, 0):
        return "B"
    raise ValueError("hf cache version is outside the supported parser range")


def _contained_snapshot(value: Any, cache_root: Path) -> Path:
    path_text = _safe_text(value, "snapshot_path")
    snapshot = Path(path_text)
    if not snapshot.is_absolute() or str(snapshot).startswith((r"\\", "//")):
        raise ValueError("hf snapshot_path must be an absolute local path")
    try:
        common = os.path.commonpath((os.path.abspath(cache_root), os.path.abspath(snapshot)))
    except ValueError as error:
        raise ValueError("hf snapshot_path is on another volume") from error
    if os.path.normcase(common) != os.path.normcase(os.path.abspath(cache_root)):
        raise ValueError("hf snapshot_path escapes the configured cache root")
    return snapshot


def _safe_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 32_767 or not _is_safe_text(value):
        raise ValueError(f"hf cache {field} is not safe text")
    return value


def _is_safe_text(value: str) -> bool:
    return not any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
    )


def _parse_human_size(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("hf current size must be display text")
    match = _HUMAN_SIZE.fullmatch(value)
    if match is None:
        raise ValueError("hf current size has an unsupported format")
    try:
        amount = Decimal(match.group(1)) * _SIZE_FACTORS[match.group(2)]
    except InvalidOperation as error:
        raise ValueError("hf current size is invalid") from error
    return int(amount)


def _failure(
    executable: Path,
    evidence: list[CommandEvidence],
    code: str,
    message: str,
    status: ProbeStatus = ProbeStatus.ERROR,
    version: str | None = None,
) -> InventoryResult:
    probe = ProbeResult(
        "huggingface", status, version, str(executable), message
    )
    return InventoryResult(
        "huggingface",
        probe,
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = ["HuggingFaceAdapter", "discover_hub_root", "parse_cache_inventory"]
