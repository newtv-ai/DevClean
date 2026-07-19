"""Read-only VS Code user-extension inventory without invoking VS Code CLIs.

The ``code`` and ``code-insiders`` CLIs are intentionally outside this adapter's
capability set: even apparently observational invocations may perform extension
housekeeping.  This module only enumerates immediate children of approved user
extension roots, reads a bounded ``package.json`` from ordinary directories, and
uses the shared metadata-only scanner for aggregate sizes.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
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
from devclean.adapters.filesystem_inventory import measure_tree
from devclean.adapters.json_contract import strict_json_loads
from devclean.core.models import (
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    new_id,
    utc_now,
)
from devclean.platform.windows.filesystem import FileSystemMetadata, read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_MAX_MANIFEST_BYTES = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ExtensionRoot:
    path: Path
    channel: str
    source: str


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    name: str
    publisher: str
    version: str
    vscode_engine: str
    digest: str


class VSCodeExtensionAdapter:
    """Inventory user-installed extensions through the filesystem only."""

    id = "vscode"

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(os.environ if environment is None else environment)

    def inventory(self, context: AdapterContext) -> InventoryResult:
        del context  # This adapter never starts a vendor process or records command output.
        roots, discovery_issues = discover_extension_roots(self.environment)
        resources: list[Resource] = []
        issues = list(discovery_issues)

        for root in roots:
            root_resources, root_issues = _inventory_root(root)
            resources.extend(root_resources)
            issues.extend(root_issues)

        if not roots:
            issues.append(
                AdapterIssue(
                    "NO_EXTENSION_ROOT",
                    "No existing safe VS Code user-extension root was found.",
                )
            )

        probe = ProbeResult(
            self.id,
            ProbeStatus.AVAILABLE if roots else ProbeStatus.UNAVAILABLE,
            detail=(
                f"filesystem-only inventory examined {len(roots)} user-extension roots; "
                "no VS Code CLI was launched"
            ),
        )
        return InventoryResult(
            self.id,
            probe,
            resources=tuple(resources),
            issues=tuple(issues),
        )


def discover_extension_roots(
    environment: Mapping[str, str],
) -> tuple[tuple[ExtensionRoot, ...], tuple[AdapterIssue, ...]]:
    """Return existing, non-reparse user-extension roots on fixed local volumes."""

    candidates: list[ExtensionRoot] = []
    issues: list[AdapterIssue] = []
    profile_text = environment.get("USERPROFILE")
    if profile_text:
        profile = Path(profile_text)
        if profile.is_absolute() and not str(profile).startswith((r"\\", "//")):
            candidates.extend(
                (
                    ExtensionRoot(
                        profile / ".vscode" / "extensions",
                        "stable",
                        "USERPROFILE",
                    ),
                    ExtensionRoot(
                        profile / ".vscode-insiders" / "extensions",
                        "insiders",
                        "USERPROFILE",
                    ),
                )
            )

    custom_text = environment.get("VSCODE_EXTENSIONS")
    if custom_text:
        custom = Path(custom_text)
        if not custom.is_absolute() or str(custom).startswith((r"\\", "//")):
            issues.append(
                AdapterIssue(
                    "CUSTOM_ROOT_UNSAFE",
                    "VSCODE_EXTENSIONS was ignored because it is not an absolute local path.",
                )
            )
        elif not is_local_fixed_path(custom):
            issues.append(
                AdapterIssue(
                    "CUSTOM_ROOT_UNSAFE",
                    "VSCODE_EXTENSIONS was ignored because it is not on a non-reparse fixed "
                    "local path.",
                )
            )
        else:
            candidates.append(ExtensionRoot(custom, "custom", "VSCODE_EXTENSIONS"))

    roots: list[ExtensionRoot] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = candidate.path
        key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        if key in seen or not is_local_fixed_path(path):
            continue
        try:
            metadata = read_file_metadata(path)
        except FileNotFoundError:
            continue
        except OSError:
            issues.append(
                AdapterIssue(
                    "ROOT_METADATA_UNAVAILABLE",
                    f"A {candidate.channel} extension root could not be inspected safely.",
                )
            )
            continue
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or metadata.is_cloud_placeholder
        ):
            issues.append(
                AdapterIssue(
                    "ROOT_BOUNDARY_REJECTED",
                    f"A {candidate.channel} extension root is a reparse/cloud boundary and "
                    "was not entered.",
                )
            )
            continue
        seen.add(key)
        roots.append(candidate)

    roots.sort(key=lambda item: os.path.normcase(str(item.path)))
    return tuple(roots), tuple(issues)


def parse_extension_manifest(data: bytes) -> ExtensionManifest:
    """Parse one bounded VS Code extension manifest with safe display fields."""

    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("VS Code extension manifest exceeds the byte limit")
    payload = strict_json_loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("VS Code extension manifest root must be an object")

    name = _manifest_identifier(payload.get("name"), "name")
    publisher = _manifest_identifier(payload.get("publisher"), "publisher")
    version = _manifest_text(payload.get("version"), "version")
    engines = payload.get("engines")
    if not isinstance(engines, dict):
        raise ValueError("VS Code extension manifest engines must be an object")
    vscode_engine = _manifest_text(engines.get("vscode"), "engines.vscode")
    return ExtensionManifest(
        name=name,
        publisher=publisher,
        version=version,
        vscode_engine=vscode_engine,
        digest=hashlib.sha256(data).hexdigest(),
    )


def _inventory_root(
    root: ExtensionRoot,
) -> tuple[list[Resource], list[AdapterIssue]]:
    resources: list[Resource] = []
    issues: list[AdapterIssue] = []
    try:
        with os.scandir(root.path) as entries:
            ordered = sorted(entries, key=lambda entry: os.path.normcase(entry.name))
    except OSError:
        return [], [
            AdapterIssue(
                "ROOT_ENUMERATION_FAILED",
                f"The {root.channel} extension root could not be enumerated.",
            )
        ]

    for entry in ordered:
        path = Path(entry.path)
        if entry.name == ".obsolete":
            marker_resource, marker_issues = _obsolete_marker(root, path)
            if marker_resource is not None:
                resources.append(marker_resource)
            issues.extend(marker_issues)
            continue

        try:
            metadata = read_file_metadata(path)
        except OSError:
            issues.append(
                AdapterIssue(
                    "EXTENSION_METADATA_UNAVAILABLE",
                    "An immediate extension-root child could not be inspected safely.",
                )
            )
            continue
        if not metadata.is_directory:
            continue
        if metadata.is_reparse_point or metadata.is_cloud_placeholder:
            issues.append(
                AdapterIssue(
                    "EXTENSION_BOUNDARY_REJECTED",
                    "A reparse/cloud extension directory was reported but not entered.",
                )
            )
            resources.append(_boundary_resource(root, path))
            continue

        resource, extension_issues = _extension_resource(root, path, metadata)
        resources.append(resource)
        issues.extend(extension_issues)
    return resources, issues


def _extension_resource(
    root: ExtensionRoot,
    path: Path,
    before: FileSystemMetadata,
) -> tuple[Resource, tuple[AdapterIssue, ...]]:
    measurement = measure_tree(path)
    issues = list(measurement.issues)
    manifest: ExtensionManifest | None = None
    try:
        manifest = _read_manifest(path / "package.json", path, before)
    except (OSError, UnicodeError, ValueError, RecursionError):
        issues.append(
            AdapterIssue(
                "MANIFEST_REJECTED",
                "An extension package.json was missing, unsafe, oversized, malformed, or "
                "changed during observation.",
            )
        )

    if manifest is None:
        display_name = "VS Code extension directory"
        semantic_type = SemanticType.UNKNOWN
        locator = None
        manifest_evidence: tuple[Evidence, ...] = ()
    else:
        display_name = f"VS Code extension {manifest.publisher}.{manifest.name}"
        semantic_type = SemanticType.APP_STATE
        locator = (
            f"vscode:{root.channel}:{manifest.publisher}.{manifest.name}@{manifest.version}"
        )
        manifest_evidence = (
            Evidence(
                source="filesystem:vscode-package-json",
                detail=(
                    "Bounded package.json observation; engines.vscode="
                    f"{manifest.vscode_engine}."
                ),
                checked_at=utc_now(),
                digest=manifest.digest,
            ),
        )

    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="vscode",
        display_name=display_name,
        semantic_type=semantic_type,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        vendor_locator=locator,
        path=str(path),
        logical_size=measurement.logical_size,
        allocated_size=measurement.allocated_size,
        reconstruction=Reconstruction.NONE,
        warnings=(
            "Installed extensions can be local, private, unpublished, or version-pinned; "
            "reconstruction is not assumed.",
            "The VS Code CLI was deliberately not invoked because inventory-like commands may "
            "perform extension housekeeping.",
            "This directory is report-only and cannot authorize a filesystem action.",
        ),
        evidence=manifest_evidence,
        actionable=False,
    )
    return resource, tuple(issues)


def _read_manifest(
    manifest_path: Path,
    extension_path: Path,
    extension_before: FileSystemMetadata,
) -> ExtensionManifest:
    manifest_before = read_file_metadata(manifest_path)
    if (
        manifest_before.is_directory
        or manifest_before.is_reparse_point
        or manifest_before.is_cloud_placeholder
        or manifest_before.logical_size > _MAX_MANIFEST_BYTES
    ):
        raise ValueError("VS Code extension manifest is not a bounded ordinary file")

    portable_stat = os.stat(manifest_path, follow_symlinks=False)
    if not stat.S_ISREG(portable_stat.st_mode):
        raise ValueError("VS Code extension manifest is not a regular file")
    with manifest_path.open("rb") as stream:
        data = stream.read(_MAX_MANIFEST_BYTES + 1)
    if len(data) > _MAX_MANIFEST_BYTES:
        raise ValueError("VS Code extension manifest exceeds the byte limit")

    manifest_after = read_file_metadata(manifest_path)
    extension_after = read_file_metadata(extension_path)
    if not _same_object(manifest_before, manifest_after) or not _same_object(
        extension_before, extension_after
    ):
        raise ValueError("VS Code extension changed while its manifest was read")
    if (
        manifest_after.is_reparse_point
        or manifest_after.is_cloud_placeholder
        or extension_after.is_reparse_point
        or extension_after.is_cloud_placeholder
    ):
        raise ValueError("VS Code extension crossed a reparse/cloud boundary")
    return parse_extension_manifest(data)


def _same_object(before: FileSystemMetadata, after: FileSystemMetadata) -> bool:
    if before.identity is None or after.identity is None:
        return False
    return before.identity == after.identity and before.is_directory == after.is_directory


def _boundary_resource(root: ExtensionRoot, path: Path) -> Resource:
    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="vscode",
        display_name="VS Code extension boundary",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=str(path),
        warnings=(
            f"Immediate child of the {root.channel} extension root was treated as a traversal "
            "boundary.",
            "The boundary target was not entered or measured.",
            "This observation is report-only and cannot authorize a filesystem action.",
        ),
        evidence=(
            Evidence(
                source="filesystem:vscode-extension-boundary",
                detail="No-follow metadata observation of an immediate extension-root child.",
                checked_at=utc_now(),
            ),
        ),
        actionable=False,
    )


def _obsolete_marker(
    root: ExtensionRoot, path: Path
) -> tuple[Resource | None, tuple[AdapterIssue, ...]]:
    try:
        metadata = read_file_metadata(path)
    except OSError:
        return None, (
            AdapterIssue(
                "OBSOLETE_METADATA_UNAVAILABLE",
                "The .obsolete marker could not be inspected safely.",
            ),
        )
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        return _boundary_resource(root, path), (
            AdapterIssue(
                "OBSOLETE_BOUNDARY_REJECTED",
                "The .obsolete marker is a reparse/cloud boundary and was not read.",
            ),
        )
    if metadata.is_directory:
        return _boundary_resource(root, path), (
            AdapterIssue(
                "OBSOLETE_NOT_FILE",
                "The .obsolete entry is a directory and was not entered.",
            ),
        )

    measurement = measure_tree(path)
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="vscode",
        display_name="VS Code .obsolete marker",
        semantic_type=SemanticType.APP_STATE,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        vendor_locator=f"vscode:{root.channel}:.obsolete",
        path=str(path),
        logical_size=measurement.logical_size,
        allocated_size=measurement.allocated_size,
        reconstruction=Reconstruction.NONE,
        warnings=(
            ".obsolete was observed as application state but its contents were not interpreted.",
            "An obsolete marker is not proof that an extension directory is safe to remove.",
            "This marker is report-only and cannot authorize a filesystem action.",
        ),
        evidence=(
            Evidence(
                source="filesystem:vscode-obsolete-marker",
                detail="Metadata-only observation; marker contents were not read.",
                checked_at=utc_now(),
            ),
        ),
        actionable=False,
    )
    return resource, measurement.issues


def _manifest_identifier(value: Any, field: str) -> str:
    text = _manifest_text(value, field)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"VS Code extension manifest {field} is not a safe identifier")
    return text


def _manifest_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or unicodedata.category(character) in {"Cf", "Cs"}
            for character in value
        )
    ):
        raise ValueError(f"VS Code extension manifest {field} is not safe text")
    return value


__all__ = [
    "ExtensionManifest",
    "ExtensionRoot",
    "VSCodeExtensionAdapter",
    "discover_extension_roots",
    "parse_extension_manifest",
]
