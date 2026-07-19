"""npm cache-root inventory without invoking cmd shims or maintenance commands."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

from devclean.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from devclean.adapters.command import QueryCommand, decode_utf8
from devclean.adapters.discovery import (
    format_version,
    parse_single_local_path,
    parse_version,
    resolve_executable,
)
from devclean.adapters.filesystem_inventory import measure_tree
from devclean.core.models import (
    EffectClass,
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    new_id,
)
from devclean.evidence.models import CommandEvidence
from devclean.evidence.redaction import redact_secrets
from devclean.platform.windows.process import ProcessLimits
from devclean.platform.windows.volumes import is_local_fixed_path

_SHORT_LIMITS = ProcessLimits(
    timeout_seconds=10,
    max_stdout_bytes=64 * 1024,
    max_stderr_bytes=64 * 1024,
)
_LIST_LIMITS = ProcessLimits(
    timeout_seconds=60,
    max_stdout_bytes=16 * 1024 * 1024,
    max_stderr_bytes=1024 * 1024,
)
_FIXED_OPTIONS = (
    "--logs-max=0",
    "--timing=false",
    "--update-notifier=false",
    "--offline",
    "--color=false",
    "--unicode=false",
    "--loglevel=silent",
)
_ENVIRONMENT = (
    ("NPM_CONFIG_AUDIT", "false"),
    ("NPM_CONFIG_FUND", "false"),
    ("NPM_CONFIG_OFFLINE", "true"),
    ("NPM_CONFIG_UPDATE_NOTIFIER", "false"),
)


class NpmCacheAdapter:
    id = "npm"

    def __init__(self, node: Path | None = None, npm_cli: Path | None = None) -> None:
        self.node = node
        self.npm_cli = npm_cli

    def inventory(self, context: AdapterContext) -> InventoryResult:
        pair = _validated_pair(self.node, self.npm_cli)
        if pair is None:
            return InventoryResult(
                self.id,
                ProbeResult(
                    self.id,
                    ProbeStatus.UNAVAILABLE,
                    detail="A direct node.exe + npm-cli.js installation was not found.",
                ),
            )
        node, npm_cli = pair
        evidence: list[CommandEvidence] = []
        try:
            version_observation = context.observe(
                _command(node, npm_cli, ("--version",), _SHORT_LIMITS)
            )
            evidence.append(version_observation.evidence)
            if not version_observation.result.succeeded:
                return _failure(node, evidence, "VERSION_QUERY_FAILED", "npm --version failed.")
            version = parse_version(decode_utf8(version_observation.result))
            if version is None or version < (8, 0, 0) or version >= (12, 0, 0):
                version_text = None if version is None else format_version(version)
                return _failure(
                    node,
                    evidence,
                    "UNSUPPORTED_VERSION",
                    "npm cache inventory supports versions >=8 and <12.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )
            version_text = format_version(version)

            help_observation = context.observe(
                _command(node, npm_cli, ("cache", "--help"), _SHORT_LIMITS)
            )
            evidence.append(help_observation.evidence)
            help_text = decode_utf8(help_observation.result)
            if not help_observation.result.succeeded or "ls" not in help_text:
                return _failure(
                    node,
                    evidence,
                    "CAPABILITY_MISSING",
                    "npm cache help does not expose cache ls.",
                    ProbeStatus.UNSUPPORTED_VERSION,
                    version_text,
                )

            config_observation = context.observe(
                _command(node, npm_cli, ("config", "get", "cache"), _SHORT_LIMITS)
            )
            evidence.append(config_observation.evidence)
            if not config_observation.result.succeeded:
                return _failure(
                    node,
                    evidence,
                    "CACHE_DIR_FAILED",
                    "npm config get cache failed.",
                    version=version_text,
                )
            cache_root = parse_single_local_path(decode_utf8(config_observation.result))
            probe = ProbeResult(
                self.id,
                ProbeStatus.AVAILABLE,
                version_text,
                str(node),
                "direct Node/npm cache query capability verified",
            )
            if not cache_root.exists():
                return InventoryResult(
                    self.id,
                    probe,
                    issues=(
                        AdapterIssue(
                            "CACHE_ROOT_MISSING",
                            "npm cache root does not exist; inventory is empty.",
                        ),
                    ),
                    evidence=tuple(evidence),
                )

            issues: list[AdapterIssue] = []
            index_evidence: Evidence | None = None
            list_observation = context.observe(
                _command(node, npm_cli, ("cache", "ls"), _LIST_LIMITS)
            )
            evidence.append(list_observation.evidence)
            if list_observation.result.succeeded:
                try:
                    key_count = parse_cache_keys(decode_utf8(list_observation.result))
                    issues.append(
                        AdapterIssue(
                            "INDEX_KEYS_OBSERVED",
                            f"npm cache ls reported {key_count} index keys; keys are not exported.",
                        )
                    )
                    index_evidence = Evidence(
                        source=f"evidence:{list_observation.evidence.evidence_id}",
                        detail="npm cache ls index-key output",
                        checked_at=list_observation.evidence.captured_at,
                        digest=list_observation.evidence.stdout_sha256,
                    )
                except (UnicodeError, ValueError):
                    issues.append(
                        AdapterIssue(
                            "INDEX_OUTPUT_REJECTED",
                            "npm cache ls output was unsafe or malformed; root occupancy remains "
                            "report-only.",
                        )
                    )
            else:
                issues.append(
                    AdapterIssue(
                        "INDEX_QUERY_FAILED",
                        "npm cache ls failed; root occupancy remains report-only and incomplete.",
                    )
                )

            resources, measurement_issues = _inventory_components(
                cache_root, version_text, index_evidence
            )
            issues.extend(measurement_issues)
            return InventoryResult(
                self.id,
                probe,
                resources=resources,
                issues=tuple(issues),
                evidence=tuple(evidence),
            )
        except (OSError, RuntimeError, UnicodeError, ValueError) as error:
            return _failure(
                node,
                evidence,
                "ADAPTER_ERROR",
                f"npm cache inventory failed closed: {type(error).__name__}.",
            )


def parse_cache_keys(text: str) -> int:
    if not text.strip():
        return 0
    count = 0
    for line in text.splitlines():
        if not line or len(line) > 65_536 or _has_control(line):
            raise ValueError("npm cache ls contains an unsafe line")
        if redact_secrets(line) != line:
            raise ValueError("npm cache ls contains credential-shaped text")
        count += 1
        if count > 1_000_000:
            raise ValueError("npm cache ls contains too many keys")
    return count


def discover_npm_pair() -> tuple[Path, Path] | None:
    node = resolve_executable("node")
    if node is None:
        return None
    npm_cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return _validated_pair(node, npm_cli)


def _validated_pair(
    node: Path | None, npm_cli: Path | None
) -> tuple[Path, Path] | None:
    if node is None and npm_cli is None:
        discovered = discover_npm_pair()
        return discovered
    if node is None or npm_cli is None:
        return None
    try:
        resolved_node = node.resolve(strict=True)
        resolved_cli = npm_cli.resolve(strict=True)
    except OSError:
        return None
    if (
        resolved_node.suffix.lower() != ".exe"
        or resolved_cli.name.lower() != "npm-cli.js"
        or not is_local_fixed_path(resolved_node)
        or not is_local_fixed_path(resolved_cli)
    ):
        return None
    try:
        common = os.path.commonpath((resolved_node.parent, resolved_cli))
    except ValueError:
        return None
    if os.path.normcase(common) != os.path.normcase(str(resolved_node.parent)):
        return None
    return (resolved_node, resolved_cli)


def _inventory_components(
    cache_root: Path,
    version: str,
    index_evidence: Evidence | None,
) -> tuple[tuple[Resource, ...], tuple[AdapterIssue, ...]]:
    resources: list[Resource] = []
    issues: list[AdapterIssue] = []
    with os.scandir(cache_root) as entries:
        top_level = [Path(entry.path) for entry in entries]
    if len(top_level) > 10_000:
        raise ValueError("npm cache root has too many top-level entries")
    for path in top_level:
        name = path.name.lower()
        if name == "_cacache":
            semantic = SemanticType.REBUILDABLE_CACHE
            risk = RiskTier.YELLOW
            provenance = ProvenanceClass.UNKNOWN
            reconstruction = Reconstruction.REDOWNLOAD_BEST_EFFORT
            label = "npm content-addressable cache"
        elif name == "_npx":
            semantic = SemanticType.PACKAGE_STORE
            risk = RiskTier.YELLOW
            provenance = ProvenanceClass.UNKNOWN
            reconstruction = Reconstruction.REBUILD_BEST_EFFORT
            label = "npm npx package cache"
        elif name == "_logs":
            semantic = SemanticType.APP_STATE
            risk = RiskTier.RED
            provenance = ProvenanceClass.LOCAL_ONLY
            reconstruction = Reconstruction.NONE
            label = "npm diagnostic logs"
        else:
            semantic = SemanticType.UNKNOWN
            risk = RiskTier.RED
            provenance = ProvenanceClass.UNKNOWN
            reconstruction = Reconstruction.NONE
            label = "Unknown npm cache entry"
        measurement = measure_tree(path)
        issues.extend(measurement.issues)
        resource_evidence = () if index_evidence is None else (index_evidence,)
        resources.append(
            Resource(
                candidate_id=new_id("candidate"),
                adapter_id="npm",
                display_name=label,
                semantic_type=semantic,
                risk_tier=risk,
                provenance_class=provenance,
                vendor_locator=f"npm:{version}:{path.name}",
                path=str(path),
                logical_size=measurement.logical_size,
                allocated_size=measurement.allocated_size,
                reconstruction=reconstruction,
                reconstruction_preconditions=(
                    "Original registry and exact package artifacts remain available.",
                    "Private-registry credentials remain valid.",
                )
                if reconstruction is not Reconstruction.NONE
                else (),
                warnings=(
                    "npm cache ls does not report sizes, orphan blobs, or reclaimable bytes.",
                    "Measured occupancy is not exclusive host reclaimable space.",
                ),
                evidence=resource_evidence,
                actionable=False,
            )
        )
    return (tuple(resources), tuple(issues))


def _command(
    node: Path,
    npm_cli: Path,
    arguments: tuple[str, ...],
    limits: ProcessLimits,
) -> QueryCommand:
    return QueryCommand(
        "npm",
        node,
        (str(npm_cli), *_FIXED_OPTIONS, *arguments),
        EffectClass.PURE_QUERY,
        limits,
        _ENVIRONMENT,
    )


def _has_control(value: str) -> bool:
    return any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or unicodedata.category(character) in {"Cf", "Cs"}
        for character in value
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
        "npm",
        ProbeResult("npm", status, version, str(executable), message),
        issues=(AdapterIssue(code, message, True),),
        evidence=tuple(evidence),
    )


__all__ = ["NpmCacheAdapter", "discover_npm_pair", "parse_cache_keys"]
