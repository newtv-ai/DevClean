"""Conda cache dry-run inventory with strict 23.7-26.x JSON contracts."""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devclean.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from devclean.adapters.command import QueryCommand, decode_utf8
from devclean.adapters.discovery import (
    VersionTuple,
    format_version,
    parse_version,
    resolve_executable,
)
from devclean.adapters.filesystem_inventory import measure_tree
from devclean.adapters.json_contract import strict_json_loads
from devclean.core.models import (
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
from devclean.evidence.models import CommandEvidence
from devclean.platform.windows.process import ProcessLimits
from devclean.platform.windows.volumes import is_local_fixed_path

_CATEGORIES = ("index_cache", "tarballs", "logfiles", "packages")
_FLAGS = {
    "index_cache": "--index-cache",
    "tarballs": "--tarballs",
    "logfiles": "--logfiles",
    "packages": "--packages",
}
_SHORT_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=64 * 1024,
)
_DRY_RUN_LIMITS = ProcessLimits(
    timeout_seconds=120,
    max_stdout_bytes=32 * 1024 * 1024,
    max_stderr_bytes=1024 * 1024,
)
_ENVIRONMENT = (
    ("CONDA_NO_PLUGINS", "true"),
    ("CONDA_OFFLINE", "true"),
    ("CONDA_NOTIFY_OUTDATED_CONDA", "false"),
    ("CONDA_REPORT_ERRORS", "false"),
    ("PYTHONDONTWRITEBYTECODE", "1"),
)


@dataclass(frozen=True, slots=True)
class CondaCandidate:
    category: str
    path: Path
    vendor_size: int | None


@dataclass(frozen=True, slots=True)
class CondaPreview:
    category: str
    candidates: tuple[CondaCandidate, ...]
    total_size: int | None


class CondaCacheAdapter:
    id = "conda"

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable

    def inventory(self, context: AdapterContext) -> InventoryResult:
        executable = self.executable or resolve_executable("conda")
        if executable is None:
            return InventoryResult(
                self.id,
                ProbeResult(self.id, ProbeStatus.UNAVAILABLE, detail="conda.exe not found"),
            )
        metadata_version = read_conda_metadata_version(executable)
        if metadata_version is None:
            return InventoryResult(
                self.id,
                ProbeResult(
                    self.id,
                    ProbeStatus.UNSUPPORTED_VERSION,
                    executable=str(executable),
                    detail=(
                        "Conda package metadata was not found; refusing to load an unknown "
                        "plugin-capable CLI."
                    ),
                ),
                issues=(
                    AdapterIssue(
                        "VERSION_METADATA_MISSING",
                        "Conda version could not be verified without launching the CLI.",
                        True,
                    ),
                ),
            )
        version_text = format_version(metadata_version)
        if metadata_version < (23, 7, 0) or metadata_version >= (27, 0, 0):
            return InventoryResult(
                self.id,
                ProbeResult(
                    self.id,
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                    str(executable),
                    "Conda --no-plugins inventory supports versions >=23.7 and <27.",
                ),
            )

        evidence: list[CommandEvidence] = []
        try:
            version_observation = context.observe(
                _command(executable, ("--version",), _SHORT_LIMITS)
            )
            evidence.append(version_observation.evidence)
            if not version_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "VERSION_QUERY_FAILED",
                    "conda --no-plugins --version failed.",
                    version_text,
                )
            runtime_version = parse_version(decode_utf8(version_observation.result))
            if runtime_version != metadata_version:
                return _failure(
                    executable,
                    evidence,
                    "VERSION_MISMATCH",
                    "Conda runtime version does not match adjacent package metadata.",
                    version_text,
                )

            help_observation = context.observe(
                _command(executable, ("clean", "--help"), _SHORT_LIMITS)
            )
            evidence.append(help_observation.evidence)
            help_text = decode_utf8(help_observation.result)
            if not help_observation.result.succeeded or not all(
                option in help_text
                for option in (
                    "--dry-run",
                    "--json",
                    "--index-cache",
                    "--tarballs",
                    "--logfiles",
                    "--packages",
                )
            ):
                return _failure(
                    executable,
                    evidence,
                    "CAPABILITY_MISSING",
                    "Conda clean help does not expose the required dry-run categories.",
                    version_text,
                    ProbeStatus.UNSUPPORTED_VERSION,
                )

            config_observation = context.observe(
                _command(
                    executable,
                    ("config", "--show", "pkgs_dirs", "--json"),
                    _SHORT_LIMITS,
                )
            )
            evidence.append(config_observation.evidence)
            if not config_observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "PKGS_DIRS_FAILED",
                    "Conda pkgs_dirs query failed.",
                    version_text,
                )
            roots = parse_pkgs_dirs(decode_utf8(config_observation.result))

            previews: list[CondaPreview] = []
            category_evidence: dict[str, CommandEvidence] = {}
            for category in _CATEGORIES:
                observation = context.observe(
                    _command(
                        executable,
                        ("clean", _FLAGS[category], "--dry-run", "--json"),
                        _DRY_RUN_LIMITS,
                    )
                )
                evidence.append(observation.evidence)
                if not observation.result.succeeded:
                    return _failure(
                        executable,
                        evidence,
                        "DRY_RUN_FAILED",
                        f"Conda {category} dry-run failed.",
                        version_text,
                    )
                previews.append(
                    parse_clean_preview(
                        decode_utf8(observation.result), category, roots
                    )
                )
                category_evidence[category] = observation.evidence

            resources: list[Resource] = []
            issues: list[AdapterIssue] = [
                AdapterIssue(
                    "OPERATIONAL_WRITES",
                    "Conda dry-run may open cache magic files with write access; no target "
                    "deletion was requested.",
                ),
                AdapterIssue(
                    "HARDLINK_EXCLUSIONS_POSSIBLE",
                    "Conda may silently exclude hard-linked entries from clean candidates.",
                ),
            ]
            for preview in previews:
                for candidate in preview.candidates:
                    resource, measurement_issues = _candidate_resource(
                        candidate, category_evidence[candidate.category]
                    )
                    resources.append(resource)
                    issues.extend(measurement_issues)
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(executable),
                "--no-plugins dry-run JSON contract verified",
            )
            return InventoryResult(
                self.id,
                probe,
                resources=tuple(resources),
                issues=tuple(issues),
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                executable,
                evidence,
                "ADAPTER_ERROR",
                f"Conda cache inventory failed closed: {type(error).__name__}.",
                version_text,
            )


def read_conda_metadata_version(executable: Path) -> VersionTuple | None:
    if (
        not executable.is_absolute()
        or not executable.is_file()
        or not is_local_fixed_path(executable)
    ):
        return None
    prefixes = (executable.parent, executable.parent.parent)
    versions: set[VersionTuple] = set()
    for prefix in prefixes:
        metadata_dir = prefix / "conda-meta"
        if not metadata_dir.is_dir() or not is_local_fixed_path(metadata_dir):
            continue
        for metadata_path in metadata_dir.glob("conda-*.json"):
            try:
                payload = strict_json_loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return None
            if not isinstance(payload, dict) or payload.get("name") != "conda":
                continue
            value = payload.get("version")
            if not isinstance(value, str):
                return None
            parsed = parse_version(value)
            if parsed is None:
                return None
            versions.add(parsed)
    return max(versions) if versions else None


def parse_pkgs_dirs(text: str) -> tuple[Path, ...]:
    payload = strict_json_loads(text)
    if not isinstance(payload, dict) or set(payload) != {"pkgs_dirs"}:
        raise ValueError("Conda pkgs_dirs JSON has an unexpected shape")
    values = payload["pkgs_dirs"]
    if not isinstance(values, list) or not values:
        raise ValueError("Conda pkgs_dirs must be a non-empty array")
    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = _local_path(value, "pkgs_dir")
        if not is_local_fixed_path(path):
            raise ValueError("Conda pkgs_dir is not on an approved fixed local volume")
        key = os.path.normcase(os.path.normpath(path))
        if key not in seen:
            seen.add(key)
            roots.append(path)
    return tuple(roots)


def parse_clean_preview(
    text: str,
    category: str,
    approved_roots: tuple[Path, ...],
) -> CondaPreview:
    if category not in _CATEGORIES:
        raise ValueError("unknown Conda clean category")
    payload = strict_json_loads(text)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise ValueError("Conda clean JSON did not report success=true")
    if category not in payload:
        raise ValueError("Conda clean JSON is missing its category payload")
    value = payload[category]
    candidates: list[CondaCandidate] = []
    total_size: int | None = None
    if category == "index_cache":
        if not isinstance(value, dict) or set(value) != {"files"}:
            raise ValueError("Conda index_cache JSON has an unexpected shape")
        files = value["files"]
        if not isinstance(files, list):
            raise ValueError("Conda index_cache files must be an array")
        for path_value in files:
            path = _approved_path(path_value, approved_roots)
            if path.name.lower() != "cache" or path.parent not in approved_roots:
                raise ValueError("Conda index_cache path is not a direct pkgs_dir/cache root")
            candidates.append(CondaCandidate(category, path, None))
    elif category == "logfiles":
        if not isinstance(value, list):
            raise ValueError("Conda logfiles must be an array")
        for path_value in value:
            path = _approved_path(path_value, approved_roots)
            if path.parent.name.lower() != ".logs" or path.parent.parent not in approved_roots:
                raise ValueError("Conda logfile is not directly under pkgs_dir/.logs")
            candidates.append(CondaCandidate(category, path, None))
    else:
        if not isinstance(value, dict):
            raise ValueError("Conda package candidate payload must be an object")
        required = {"warnings", "pkg_sizes", "pkgs_dirs", "total_size"}
        if not required.issubset(value):
            raise ValueError("Conda package candidate payload is missing required fields")
        warnings = value["warnings"]
        if not isinstance(warnings, list) or not all(
            isinstance(warning, str) for warning in warnings
        ):
            raise ValueError("Conda warnings must be a string array")
        if warnings:
            raise ValueError("Conda dry-run reported incomplete size warnings")
        sizes = value["pkg_sizes"]
        directories = value["pkgs_dirs"]
        total_size = value["total_size"]
        if (
            not isinstance(sizes, dict)
            or not isinstance(directories, dict)
            or isinstance(total_size, bool)
            or not isinstance(total_size, int)
            or total_size < 0
        ):
            raise ValueError("Conda candidate maps or total_size have invalid types")
        if set(sizes) != set(directories):
            raise ValueError("Conda pkg_sizes and pkgs_dirs roots differ")
        computed_total = 0
        for root_text, name_sizes in sizes.items():
            root = _approved_path(root_text, approved_roots)
            if root not in approved_roots:
                raise ValueError("Conda package map root is not an approved pkgs_dir")
            names = directories[root_text]
            if not isinstance(name_sizes, dict) or not isinstance(names, list):
                raise ValueError("Conda package map values have invalid types")
            if set(name_sizes) != set(names) or not all(isinstance(name, str) for name in names):
                raise ValueError("Conda package names differ between maps")
            for name in names:
                if not _safe_basename(name):
                    raise ValueError("Conda package candidate name is unsafe")
                size = name_sizes[name]
                if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                    raise ValueError("Conda package candidate size is invalid")
                computed_total += size
                candidates.append(CondaCandidate(category, root / name, size))
        if computed_total != total_size:
            raise ValueError("Conda total_size does not equal the candidate size sum")
    return CondaPreview(category, tuple(candidates), total_size)


def _candidate_resource(
    candidate: CondaCandidate, evidence_record: CommandEvidence
) -> tuple[Resource, tuple[AdapterIssue, ...]]:
    measurement = measure_tree(candidate.path)
    if candidate.vendor_size is None:
        logical_size = measurement.logical_size
        vendor_reclaimable = measurement.logical_size
    else:
        logical_size = SizeValue(candidate.vendor_size, Confidence.ESTIMATE)
        vendor_reclaimable = SizeValue(candidate.vendor_size, Confidence.ESTIMATE)
    if candidate.category == "packages":
        semantic = SemanticType.PACKAGE_STORE
        risk = RiskTier.YELLOW
        provenance = ProvenanceClass.UNKNOWN
        reconstruction = Reconstruction.REDOWNLOAD_BEST_EFFORT
    elif candidate.category == "logfiles":
        semantic = SemanticType.APP_STATE
        risk = RiskTier.RED
        provenance = ProvenanceClass.LOCAL_ONLY
        reconstruction = Reconstruction.NONE
    else:
        semantic = SemanticType.REBUILDABLE_CACHE
        risk = RiskTier.YELLOW
        provenance = ProvenanceClass.UNKNOWN
        reconstruction = Reconstruction.REDOWNLOAD_BEST_EFFORT
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="conda",
        display_name=f"Conda {candidate.category} dry-run candidate",
        semantic_type=semantic,
        risk_tier=risk,
        provenance_class=provenance,
        vendor_locator=f"conda:{candidate.category}:{candidate.path.name}",
        path=str(candidate.path),
        logical_size=logical_size,
        allocated_size=measurement.allocated_size,
        vendor_logical_reclaimable=vendor_reclaimable,
        reconstruction=reconstruction,
        reconstruction_preconditions=(
            "Original channels and exact packages remain available.",
            "Private-channel credentials remain valid.",
        )
        if reconstruction is not Reconstruction.NONE
        else (),
        warnings=(
            "Conda dry-run is vendor policy evidence, not an execution authorization.",
            "Vendor total is logical and may silently exclude hard-linked entries.",
            "Exclusive host reclaimable bytes remain unknown.",
        ),
        evidence=(
            Evidence(
                source=f"evidence:{evidence_record.evidence_id}",
                detail=f"Conda {candidate.category} --dry-run --json output",
                checked_at=evidence_record.captured_at,
                digest=evidence_record.stdout_sha256,
            ),
        ),
        actionable=False,
    )
    return (resource, measurement.issues)


def _approved_path(value: Any, roots: tuple[Path, ...]) -> Path:
    path = _local_path(value, "candidate path")
    for root in roots:
        try:
            common = os.path.commonpath((os.path.abspath(root), os.path.abspath(path)))
        except ValueError:
            continue
        if os.path.normcase(common) == os.path.normcase(os.path.abspath(root)):
            return path
    raise ValueError("Conda candidate path escapes approved pkgs_dirs")


def _local_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 32_767 or _has_control(value):
        raise ValueError(f"Conda {field} is unsafe text")
    path = Path(value)
    if not path.is_absolute() or str(path).startswith((r"\\", "//")):
        raise ValueError(f"Conda {field} is not an absolute local path")
    return path


def _safe_basename(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 255
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
        and not _has_control(value)
    )


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
        "conda",
        executable,
        ("--no-plugins", *arguments),
        EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        limits,
        _ENVIRONMENT,
        ("Conda may open package-cache magic files with write access.",),
    )


def _failure(
    executable: Path,
    evidence: list[CommandEvidence],
    code: str,
    message: str,
    version: str,
    status: ProbeStatus = ProbeStatus.ERROR,
) -> InventoryResult:
    return InventoryResult(
        "conda",
        ProbeResult("conda", status, version, str(executable), message),
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = [
    "CondaCacheAdapter",
    "CondaCandidate",
    "CondaPreview",
    "parse_clean_preview",
    "parse_pkgs_dirs",
    "read_conda_metadata_version",
]
