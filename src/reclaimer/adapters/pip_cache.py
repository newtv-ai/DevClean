"""pip cache inventory for one explicitly discovered Python interpreter."""

from __future__ import annotations

import os
import unicodedata
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
)
from reclaimer.adapters.filesystem_inventory import measure_tree
from reclaimer.core.models import (
    EffectClass,
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    new_id,
)
from reclaimer.evidence.models import CommandEvidence
from reclaimer.platform.windows.process import ProcessLimits
from reclaimer.platform.windows.volumes import is_local_fixed_path

_PROBE_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=256 * 1024,
    max_stderr_bytes=256 * 1024,
)
_INFO_LIMITS = ProcessLimits(
    timeout_seconds=120,
    max_stdout_bytes=1024 * 1024,
    max_stderr_bytes=1024 * 1024,
)
_LIST_LIMITS = ProcessLimits(
    timeout_seconds=120,
    max_stdout_bytes=16 * 1024 * 1024,
    max_stderr_bytes=1024 * 1024,
)
_ENVIRONMENT = (
    ("PIP_DISABLE_PIP_VERSION_CHECK", "1"),
    ("PIP_NO_INPUT", "1"),
    ("PIP_NO_COLOR", "1"),
    ("PIP_NO_INDEX", "1"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
)
_BASE = (
    "-B",
    "-m",
    "pip",
    "--disable-pip-version-check",
    "--no-input",
    "--no-color",
)


class PipCacheAdapter:
    id = "pip"

    def __init__(self, interpreter: Path) -> None:
        self.interpreter = interpreter

    def inventory(self, context: AdapterContext) -> InventoryResult:
        executable = self.interpreter
        if (
            not executable.is_absolute()
            or not executable.is_file()
            or not is_local_fixed_path(executable)
        ):
            probe = ProbeResult(
                self.id,
                ProbeStatus.UNAVAILABLE,
                executable=str(executable),
                detail="Python interpreter is missing or not absolute.",
            )
            return InventoryResult(self.id, probe)

        evidence: list[CommandEvidence] = []
        try:
            version_observation = context.observe(
                QueryCommand(
                    self.id,
                    executable,
                    ("-B", "-m", "pip", "--version"),
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
                    "pip --version failed.",
                )
            version = parse_version(decode_utf8(version_observation.result))
            if version is None or version < (21, 0, 0) or version >= (27, 0, 0):
                version_text = None if version is None else format_version(version)
                return _failure(
                    executable,
                    evidence,
                    "UNSUPPORTED_VERSION",
                    "Full read-only cache inventory supports pip >=21 and <27.",
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
                command in help_text for command in ("dir", "info", "list")
            ):
                return _failure(
                    executable,
                    evidence,
                    "CAPABILITY_MISSING",
                    "pip cache help does not expose dir/info/list.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )

            dir_observation = context.observe(
                _command(executable, ("cache", "dir"), _PROBE_LIMITS)
            )
            evidence.append(dir_observation.evidence)
            if not dir_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_DIR_FAILED",
                    "pip cache dir failed.",
                    version=version_text,
                )
            cache_root = parse_single_local_path(decode_utf8(dir_observation.result))
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(executable),
                "pip cache dir/info/list capability verified",
            )
            if not cache_root.exists():
                return InventoryResult(
                    self.id,
                    probe,
                    issues=(
                        AdapterIssue(
                            "CACHE_ROOT_MISSING",
                            "pip cache root does not exist; inventory is empty.",
                        ),
                    ),
                    evidence=tuple(evidence),
                )

            info_observation = context.observe(
                _command(executable, ("cache", "info"), _INFO_LIMITS)
            )
            evidence.append(info_observation.evidence)
            if not info_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_INFO_FAILED",
                    "pip cache info failed.",
                    version=version_text,
                )
            decode_utf8(info_observation.result)

            list_observation = context.observe(
                _command(
                    executable,
                    ("cache", "list", "--format=abspath"),
                    _LIST_LIMITS,
                )
            )
            evidence.append(list_observation.evidence)
            if not list_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "CACHE_LIST_FAILED",
                    "pip cache list failed.",
                    version=version_text,
                )
            parse_cache_list(decode_utf8(list_observation.result), cache_root)
            measurement = measure_tree(cache_root)
            resource = Resource(
                candidate_id=new_id("candidate"),
                adapter_id=self.id,
                display_name="pip cache root",
                semantic_type=SemanticType.REBUILDABLE_CACHE,
                risk_tier=RiskTier.YELLOW,
                provenance_class=ProvenanceClass.UNKNOWN,
                vendor_locator=f"pip:{executable}",
                path=str(cache_root),
                logical_size=measurement.logical_size,
                allocated_size=measurement.allocated_size,
                reconstruction=Reconstruction.REDOWNLOAD_BEST_EFFORT,
                reconstruction_preconditions=(
                    "Original indexes and exact artifacts remain available.",
                    "Private-index credentials remain valid.",
                ),
                warnings=(
                    "pip cache list covers locally built wheels, not the complete HTTP cache.",
                    "Locally built or private artifacts may not be retrievable later.",
                    "Measured root size is occupancy, not purgeable or exclusive "
                    "reclaimable bytes.",
                ),
                evidence=(
                    Evidence(
                        source=f"evidence:{list_observation.evidence.evidence_id}",
                        detail="pip cache list --format=abspath output",
                        checked_at=list_observation.evidence.captured_at,
                        digest=list_observation.evidence.stdout_sha256,
                    ),
                ),
                actionable=False,
            )
            return InventoryResult(
                self.id,
                probe,
                resources=(resource,),
                issues=measurement.issues,
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                executable,
                evidence,
                "ADAPTER_ERROR",
                f"pip cache inventory failed closed: {type(error).__name__}.",
            )


def parse_cache_list(text: str, cache_root: Path) -> tuple[Path, ...]:
    value = text.strip()
    if not value or value == "No locally built wheels cached.":
        return ()
    paths: list[Path] = []
    root = os.path.abspath(cache_root)
    for line in value.splitlines():
        if not line or len(line) > 32_767 or _has_control(line):
            raise ValueError("pip cache list contains an unsafe line")
        path = Path(line)
        if not path.is_absolute() or str(path).startswith((r"\\", "//")):
            raise ValueError("pip cache list contains a non-local path")
        try:
            common = os.path.commonpath((root, os.path.abspath(path)))
        except ValueError as error:
            raise ValueError("pip cache list path is on another volume") from error
        if os.path.normcase(common) != os.path.normcase(root):
            raise ValueError("pip cache list path escapes the cache root")
        paths.append(path)
    return tuple(paths)


def _has_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
    )


def _command(
    executable: Path, arguments: tuple[str, ...], limits: ProcessLimits
) -> QueryCommand:
    return QueryCommand(
        "pip",
        executable,
        (*_BASE, *arguments),
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
        "pip",
        ProbeResult("pip", status, version, str(executable), message),
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = ["PipCacheAdapter", "parse_cache_list"]
