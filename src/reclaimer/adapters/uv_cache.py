"""uv cache inventory with strict experimental-size capability gating."""

from __future__ import annotations

import re
from pathlib import Path

from reclaimer.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from reclaimer.adapters.command import QueryCommand, decode_utf8
from reclaimer.adapters.discovery import (
    format_version,
    parse_single_local_path,
    parse_version,
    resolve_executable,
)
from reclaimer.adapters.filesystem_inventory import measure_tree
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

_INTEGER = re.compile(r"^[0-9]+\r?\n?$")
_PROBE_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=256 * 1024,
    max_stderr_bytes=256 * 1024,
)
_SIZE_LIMITS_FAST = ProcessLimits(
    timeout_seconds=120,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=512 * 1024,
)
_SIZE_LIMITS_SLOW = ProcessLimits(
    timeout_seconds=300,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=512 * 1024,
)
_ENVIRONMENT = (
    ("UV_OFFLINE", "1"),
    ("UV_NO_PROGRESS", "1"),
    ("UV_PYTHON_DOWNLOADS", "never"),
)
_QUERY_FLAGS = (
    "--offline",
    "--no-config",
    "--no-python-downloads",
    "--no-progress",
    "--color",
    "never",
)


class UvCacheAdapter:
    id = "uv"

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable

    def inventory(self, context: AdapterContext) -> InventoryResult:
        executable = self.executable or resolve_executable("uv")
        if executable is None or not is_local_fixed_path(executable):
            return InventoryResult(
                self.id,
                ProbeResult(self.id, ProbeStatus.UNAVAILABLE, detail="uv.exe not found"),
            )
        evidence: list[CommandEvidence] = []
        try:
            version_observation = context.observe(
                _command(executable, ("--version",), _PROBE_LIMITS)
            )
            evidence.append(version_observation.evidence)
            if not version_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "VERSION_QUERY_FAILED",
                    "uv --version failed.",
                )
            version = parse_version(decode_utf8(version_observation.result))
            if version is None or version < (0, 9, 8) or version >= (0, 12, 0):
                version_text = None if version is None else format_version(version)
                return _failure(
                    executable,
                    evidence,
                    "UNSUPPORTED_VERSION",
                    "uv cache size inventory supports versions >=0.9.8 and <0.12.0.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )
            version_text = format_version(version)

            help_observation = context.observe(
                _command(executable, ("cache", "--help"), _PROBE_LIMITS)
            )
            evidence.append(help_observation.evidence)
            help_text = decode_utf8(help_observation.result)
            if not help_observation.result.succeeded or not all(
                item in help_text for item in ("dir", "size")
            ):
                return _failure(
                    executable,
                    evidence,
                    "CAPABILITY_MISSING",
                    "uv cache help does not expose dir and size.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )

            size_help = context.observe(
                _command(executable, ("cache", "size", "--help"), _PROBE_LIMITS)
            )
            evidence.append(size_help.evidence)
            if not size_help.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "SIZE_CAPABILITY_MISSING",
                    "uv cache size help failed.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )
            decode_utf8(size_help.result)

            dir_observation = context.observe(
                _command(executable, ("cache", "dir", *_QUERY_FLAGS), _PROBE_LIMITS)
            )
            evidence.append(dir_observation.evidence)
            if not dir_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_DIR_FAILED",
                    "uv cache dir failed.",
                    version=version_text,
                )
            cache_root = parse_single_local_path(decode_utf8(dir_observation.result))
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(executable),
                "uv offline cache dir/size capability verified",
            )
            if not cache_root.exists():
                return InventoryResult(
                    self.id,
                    probe,
                    issues=(
                        AdapterIssue(
                            "CACHE_ROOT_MISSING",
                            "uv cache root does not exist; inventory is empty.",
                        ),
                    ),
                    evidence=tuple(evidence),
                )

            size_limits = (
                _SIZE_LIMITS_FAST if version >= (0, 9, 18) else _SIZE_LIMITS_SLOW
            )
            size_observation = context.observe(
                _command(
                    executable,
                    (
                        "--preview-features",
                        "cache-size",
                        "cache",
                        "size",
                        "--cache-dir",
                        str(cache_root),
                        *_QUERY_FLAGS,
                    ),
                    size_limits,
                )
            )
            evidence.append(size_observation.evidence)
            if not size_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_SIZE_FAILED",
                    "uv cache size failed, timed out, or exceeded its output limit.",
                    version=version_text,
                )
            vendor_size = parse_cache_size(decode_utf8(size_observation.result))
            measurement = measure_tree(cache_root)
            resource = Resource(
                candidate_id=new_id("candidate"),
                adapter_id=self.id,
                display_name="uv cache root",
                semantic_type=SemanticType.REBUILDABLE_CACHE,
                risk_tier=RiskTier.YELLOW,
                provenance_class=ProvenanceClass.UNKNOWN,
                vendor_locator=f"uv:{executable}",
                path=str(cache_root),
                logical_size=SizeValue(vendor_size, Confidence.ESTIMATE),
                allocated_size=measurement.allocated_size,
                reconstruction=Reconstruction.REDOWNLOAD_BEST_EFFORT,
                reconstruction_preconditions=(
                    "Original indexes, URLs, and exact artifacts remain available.",
                    "Private-source credentials remain valid.",
                ),
                warnings=(
                    "uv cache size is experimental and ignores some filesystem errors; it is "
                    "an estimate.",
                    "--no-config intentionally ignores project-level custom cache roots.",
                    "Cache occupancy is not pruneable or exclusive reclaimable bytes.",
                ),
                evidence=(
                    Evidence(
                        source=f"evidence:{size_observation.evidence.evidence_id}",
                        detail="uv experimental cache size output",
                        checked_at=size_observation.evidence.captured_at,
                        digest=size_observation.evidence.stdout_sha256,
                    ),
                ),
                actionable=False,
            )
            issues = list(measurement.issues)
            if measurement.logical_size.value != vendor_size:
                issues.append(
                    AdapterIssue(
                        "SIZE_CROSS_CHECK_DIFFERS",
                        "uv vendor size differs from Reclaimer's metadata-only root sum.",
                    )
                )
            return InventoryResult(
                self.id,
                probe,
                resources=(resource,),
                issues=tuple(issues),
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                executable,
                evidence,
                "ADAPTER_ERROR",
                f"uv cache inventory failed closed: {type(error).__name__}.",
            )


def parse_cache_size(text: str) -> int:
    if _INTEGER.fullmatch(text) is None:
        raise ValueError("uv cache size must be one non-negative integer line")
    return int(text.strip())


def _command(
    executable: Path, arguments: tuple[str, ...], limits: ProcessLimits
) -> QueryCommand:
    return QueryCommand(
        "uv",
        executable,
        arguments,
        EffectClass.PURE_QUERY,
        limits,
        _ENVIRONMENT,
    )


def _failure(
    executable: Path,
    evidence: list[CommandEvidence],
    code: str,
    message: str,
    status: ProbeStatus = ProbeStatus.ERROR,
    version: str | None = None,
) -> InventoryResult:
    return InventoryResult(
        "uv",
        ProbeResult("uv", status, version, str(executable), message),
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = ["UvCacheAdapter", "parse_cache_size"]
