"""pnpm store discovery that deliberately avoids all pnpm store commands."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from devclean.adapters.base import (
    AdapterContext,
    AdapterIssue,
    InventoryResult,
    ProbeResult,
    ProbeStatus,
)
from devclean.adapters.filesystem_inventory import measure_tree
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
from devclean.platform.windows.volumes import fixed_volume_roots, is_local_fixed_path

_VERSION_DIRECTORY = re.compile(r"^v([0-9]+)$", re.IGNORECASE)
_SUPPORTED_STORE_MAJORS = frozenset({10, 11})


class PnpmStoreAdapter:
    id = "pnpm"

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        volume_roots: Sequence[Path] | None = None,
    ) -> None:
        self.environment = dict(os.environ if environment is None else environment)
        self.volume_roots = volume_roots

    def inventory(self, context: AdapterContext) -> InventoryResult:
        del context  # The safe default path never launches pnpm or records command output.
        roots = discover_store_roots(self.environment, self.volume_roots)
        resources: list[Resource] = []
        issues: list[AdapterIssue] = [
            AdapterIssue(
                "CUSTOM_ROOTS_NOT_DISCOVERED",
                "Project-level or arbitrary custom storeDir values are not probed by the safe "
                "default inventory.",
            )
        ]
        for index, root in enumerate(roots):
            match = _VERSION_DIRECTORY.fullmatch(root.name)
            major = int(match.group(1)) if match else 0
            supported = major in _SUPPORTED_STORE_MAJORS
            measurement = measure_tree(root)
            issues.extend(measurement.issues)
            resources.append(
                Resource(
                    candidate_id=new_id("candidate"),
                    adapter_id=self.id,
                    display_name=f"pnpm store v{major}" if major else "pnpm store",
                    semantic_type=(
                        SemanticType.PACKAGE_STORE if supported else SemanticType.UNKNOWN
                    ),
                    risk_tier=RiskTier.YELLOW if supported else RiskTier.RED,
                    provenance_class=ProvenanceClass.UNKNOWN,
                    vendor_locator=f"pnpm-store:v{major}:{index}",
                    path=str(root),
                    logical_size=measurement.logical_size,
                    allocated_size=measurement.allocated_size,
                    reconstruction=(
                        Reconstruction.REDOWNLOAD_BEST_EFFORT
                        if supported
                        else Reconstruction.NONE
                    ),
                    reconstruction_preconditions=(
                        "Every consuming project still has a complete lockfile.",
                        "Original registries and private credentials remain available.",
                    )
                    if supported
                    else (),
                    warnings=(
                        "No pnpm store command was run: store path may create temporary files and "
                        "store status may write a SQLite index.",
                        "Store files may be hard-linked into projects; occupancy is not exclusive "
                        "reclaimable space.",
                        "Unknown or old store majors remain RED and report-only.",
                    ),
                    evidence=(
                        Evidence(
                            source="filesystem:pnpm-known-root",
                            detail=(
                                "Metadata-only scan of an existing conventional pnpm store root."
                            ),
                            checked_at=utc_now(),
                        ),
                    ),
                    actionable=False,
                )
            )
        probe = ProbeResult(
            self.id,
            ProbeStatus.AVAILABLE,
            detail=(
                f"metadata-only conventional-root discovery found {len(resources)} store roots"
            ),
        )
        if not resources:
            issues.append(
                AdapterIssue(
                    "NO_CONVENTIONAL_ROOT",
                    "No existing conventional pnpm store root was found.",
                )
            )
        return InventoryResult(
            self.id,
            probe,
            resources=tuple(resources),
            issues=tuple(issues),
        )


def discover_store_roots(
    environment: Mapping[str, str],
    volume_roots: Sequence[Path] | None = None,
) -> tuple[Path, ...]:
    bases: list[Path] = []
    if environment.get("PNPM_HOME"):
        bases.append(Path(environment["PNPM_HOME"]) / "store")
    if environment.get("LOCALAPPDATA"):
        bases.append(Path(environment["LOCALAPPDATA"]) / "pnpm" / "store")
    for volume in volume_roots if volume_roots is not None else fixed_volume_roots():
        bases.append(volume / ".pnpm-store")

    roots: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_absolute() or not base.is_dir() or not is_local_fixed_path(base):
            continue
        try:
            with os.scandir(base) as entries:
                candidates = [
                    Path(entry.path)
                    for entry in entries
                    if _VERSION_DIRECTORY.fullmatch(entry.name)
                    and entry.is_dir(follow_symlinks=False)
                ]
        except OSError:
            continue
        for candidate in candidates:
            if not is_local_fixed_path(candidate):
                continue
            key = os.path.normcase(os.path.normpath(candidate))
            if key not in seen:
                seen.add(key)
                roots.append(candidate)
    roots.sort(key=lambda path: os.path.normcase(str(path)))
    return tuple(roots)


__all__ = ["PnpmStoreAdapter", "discover_store_roots"]
