"""Docker local-daemon disk-usage inventory with no Desktop startup or prune."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from devclean.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from devclean.adapters.command import QueryCommand, decode_utf8
from devclean.adapters.discovery import format_version, parse_version, resolve_executable
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

_LOCAL_ENGINE = "npipe:////./pipe/docker_engine"
_EXPECTED_TYPES = frozenset({"Images", "Containers", "Local Volumes", "Build Cache"})
_SIZE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB|PB|EB)$")
_RECLAIMABLE = re.compile(
    r"^([0-9]+(?:\.[0-9]+)?(?:B|kB|MB|GB|TB|PB|EB))(?: \(([0-9]{1,3})%\))?$"
)
_FACTORS = {
    "B": 1,
    "kB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "PB": 1_000_000_000_000_000,
    "EB": 1_000_000_000_000_000_000,
}
_SHORT_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=256 * 1024,
    max_stderr_bytes=256 * 1024,
)
_INVENTORY_LIMITS = ProcessLimits(
    timeout_seconds=60,
    max_stdout_bytes=1024 * 1024,
    max_stderr_bytes=1024 * 1024,
)


@dataclass(frozen=True, slots=True)
class DockerUsage:
    resource_type: str
    total_count: int
    active_count: int
    size: int
    reclaimable: int
    reclaimable_percent: int | None


class DockerAdapter:
    id = "docker"

    def __init__(self, executable: Path | None = None) -> None:
        self.executable = executable

    def inventory(self, context: AdapterContext) -> InventoryResult:
        executable = self.executable or resolve_executable("docker")
        if executable is None or not is_local_fixed_path(executable):
            return InventoryResult(
                self.id,
                ProbeResult(self.id, ProbeStatus.UNAVAILABLE, detail="docker.exe not found"),
            )
        evidence: list[CommandEvidence] = []
        try:
            safe_config = context.evidence_store.root / "docker-config"
            safe_config.mkdir(parents=True, exist_ok=True)
            prefix = (
                "--config",
                str(safe_config),
                "--host",
                _LOCAL_ENGINE,
            )
            version_observation = context.observe(
                _command(
                    executable,
                    (
                        *prefix,
                        "version",
                        "--format",
                        "{{.Client.Version}}\t{{.Server.Version}}",
                    ),
                    _SHORT_LIMITS,
                )
            )
            evidence.append(version_observation.evidence)
            if not version_observation.result.succeeded:
                probe = ProbeResult(
                    self.id,
                    ProbeStatus.UNAVAILABLE,
                    executable=str(executable),
                    detail="The fixed local Docker named pipe is not available.",
                )
                return InventoryResult(
                    self.id,
                    probe,
                    issues=(
                        AdapterIssue(
                            "DAEMON_UNAVAILABLE",
                            "Docker was not started; local-daemon inventory was skipped.",
                        ),
                    ),
                    evidence=tuple(evidence),
                )
            client_version, server_version = parse_version_pair(
                decode_utf8(version_observation.result)
            )
            if (
                client_version < (24, 0, 0)
                or client_version >= (31, 0, 0)
                or server_version < (24, 0, 0)
                or server_version >= (31, 0, 0)
            ):
                return _failure(
                    executable,
                    evidence,
                    "UNSUPPORTED_VERSION",
                    "Docker client/server inventory supports versions >=24 and <31.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    f"{format_version(client_version)}/{format_version(server_version)}",
                )
            version_text = (
                f"{format_version(client_version)}/{format_version(server_version)}"
            )

            help_observation = context.observe(
                _command(executable, (*prefix, "system", "df", "--help"), _SHORT_LIMITS)
            )
            evidence.append(help_observation.evidence)
            help_text = decode_utf8(help_observation.result)
            if not help_observation.result.succeeded or "--format" not in help_text:
                return _failure(
                    executable,
                    evidence,
                    "CAPABILITY_MISSING",
                    "docker system df help does not expose --format.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )

            observation = context.observe(
                _command(
                    executable,
                    (*prefix, "system", "df", "--format", "json"),
                    _INVENTORY_LIMITS,
                )
            )
            evidence.append(observation.evidence)
            if not observation.result.succeeded:
                return _failure(
                    executable,
                    evidence,
                    "DISK_USAGE_FAILED",
                    "docker system df failed, timed out, or exceeded its output limit.",
                    version=version_text,
                )
            usages = parse_system_df_jsonl(decode_utf8(observation.result))
            resources = tuple(
                _usage_resource(usage, observation.evidence) for usage in usages
            )
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(executable),
                "fixed local named-pipe disk-usage query verified",
            )
            return InventoryResult(
                self.id,
                probe,
                resources=resources,
                issues=(
                    AdapterIssue(
                        "HOST_PHYSICAL_UNKNOWN",
                        "Docker daemon logical usage does not prove Windows host/VHDX space "
                        "reclamation.",
                    ),
                    AdapterIssue(
                        "SANDBOX_CONFIG_WRITE",
                        "DevClean used an empty app-data Docker config directory to avoid user "
                        "credentials and remote contexts.",
                    ),
                ),
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                executable,
                evidence,
                "ADAPTER_ERROR",
                f"Docker inventory failed closed: {type(error).__name__}.",
            )


def parse_version_pair(text: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    value = text.rstrip("\r\n")
    parts = value.split("\t")
    if len(parts) != 2:
        raise ValueError("Docker version output must contain one client/server pair")
    client = parse_version(parts[0])
    server = parse_version(parts[1])
    if client is None or server is None:
        raise ValueError("Docker client/server version is invalid")
    return (client, server)


def parse_system_df_jsonl(text: str) -> tuple[DockerUsage, ...]:
    lines = text.splitlines()
    if len(lines) != 4:
        raise ValueError("docker system df JSON must contain exactly four JSONL records")
    usages: list[DockerUsage] = []
    seen: set[str] = set()
    for line in lines:
        payload = strict_json_loads(line)
        if not isinstance(payload, dict):
            raise ValueError("Docker disk-usage record must be an object")
        required = {"Type", "TotalCount", "Active", "Size", "Reclaimable"}
        if not required.issubset(payload):
            raise ValueError("Docker disk-usage record is missing required fields")
        if not all(isinstance(payload[field], str) for field in required):
            raise ValueError("Docker disk-usage fields must be strings")
        resource_type = payload["Type"]
        if resource_type not in _EXPECTED_TYPES or resource_type in seen:
            raise ValueError("Docker disk-usage type is unknown or duplicated")
        seen.add(resource_type)
        total = _count(payload["TotalCount"])
        active = _count(payload["Active"])
        if active > total:
            raise ValueError("Docker active count exceeds total count")
        size = _human_size(payload["Size"])
        reclaimable_match = _RECLAIMABLE.fullmatch(payload["Reclaimable"])
        if reclaimable_match is None:
            raise ValueError("Docker reclaimable size has an unsupported format")
        reclaimable = _human_size(reclaimable_match.group(1))
        percent = (
            None
            if reclaimable_match.group(2) is None
            else int(reclaimable_match.group(2))
        )
        if percent is not None and percent > 100:
            raise ValueError("Docker reclaimable percentage exceeds 100")
        usages.append(
            DockerUsage(resource_type, total, active, size, reclaimable, percent)
        )
    if seen != _EXPECTED_TYPES:
        raise ValueError("Docker disk-usage record set is incomplete")
    return tuple(usages)


def _usage_resource(usage: DockerUsage, evidence: CommandEvidence) -> Resource:
    if usage.resource_type == "Images":
        semantic = SemanticType.PACKAGE_STORE
        risk = RiskTier.YELLOW
        provenance = ProvenanceClass.UNKNOWN
        reconstruction = Reconstruction.REDOWNLOAD_BEST_EFFORT
    elif usage.resource_type == "Build Cache":
        semantic = SemanticType.BUILD_OUTPUT
        risk = RiskTier.YELLOW
        provenance = ProvenanceClass.UNKNOWN
        reconstruction = Reconstruction.REBUILD_BEST_EFFORT
    elif usage.resource_type == "Containers":
        semantic = SemanticType.APP_STATE
        risk = RiskTier.RED
        provenance = ProvenanceClass.LOCAL_ONLY
        reconstruction = Reconstruction.NONE
    else:
        semantic = SemanticType.USER_DATA
        risk = RiskTier.RED
        provenance = ProvenanceClass.LOCAL_ONLY
        reconstruction = Reconstruction.NONE
    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="docker",
        display_name=f"Docker {usage.resource_type}",
        semantic_type=semantic,
        risk_tier=risk,
        provenance_class=provenance,
        vendor_locator=f"docker:{usage.resource_type}",
        logical_size=SizeValue(usage.size, Confidence.ESTIMATE),
        vendor_logical_reclaimable=SizeValue(
            usage.reclaimable, Confidence.ESTIMATE
        ),
        reconstruction=reconstruction,
        reconstruction_preconditions=(
            (
                "Original registries, Dockerfiles, contexts, and credentials remain "
                "available.",
            )
            if reconstruction is not Reconstruction.NONE
            else ()
        ),
        warnings=(
            f"Vendor summary counts total={usage.total_count}, active={usage.active_count}.",
            "Docker human-readable sizes are rounded estimates.",
            "Daemon reclaimable bytes are vendor policy, not host-physical/VHDX reclaimable bytes.",
            "No prune, stop, remove, volume, or VHD command was run.",
        ),
        evidence=(
            Evidence(
                source=f"evidence:{evidence.evidence_id}",
                detail="docker system df --format json JSONL output",
                checked_at=evidence.captured_at,
                digest=evidence.stdout_sha256,
            ),
        ),
        actionable=False,
    )


def _count(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("Docker count is not a non-negative decimal integer")
    return int(value)


def _human_size(value: str) -> int:
    match = _SIZE.fullmatch(value)
    if match is None:
        raise ValueError("Docker size has an unsupported format")
    return int(Decimal(match.group(1)) * _FACTORS[match.group(2)])


def _command(
    executable: Path, arguments: tuple[str, ...], limits: ProcessLimits
) -> QueryCommand:
    return QueryCommand(
        "docker",
        executable,
        arguments,
        EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        limits,
        known_operational_writes=(
            "DevClean creates an empty app-data Docker config sandbox.",
        ),
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
        "docker",
        ProbeResult("docker", status, version, str(executable), message),
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = [
    "DockerAdapter",
    "DockerUsage",
    "parse_system_df_jsonl",
    "parse_version_pair",
]
