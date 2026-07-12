"""Report-only official Windows maintenance guidance.

This module intentionally never launches a process or requests elevation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from reclaimer.core.models import (
    Evidence,
    ProvenanceClass,
    Reconstruction,
    Resource,
    RiskTier,
    SemanticType,
    UndoCapability,
    new_id,
)


@dataclass(frozen=True, slots=True)
class ExternalCommandGuide:
    guide_id: str
    title: str
    command: tuple[str, ...]
    description: str
    requires_administrator: bool
    official_source: str
    externally_executed: bool = True

    @property
    def display_command(self) -> str:
        return " ".join(self.command)


COMPONENT_STORE_ANALYSIS = ExternalCommandGuide(
    guide_id="windows.component_store.analyze",
    title="Analyze the Windows component store",
    command=(
        "DISM.exe",
        "/Online",
        "/Cleanup-Image",
        "/AnalyzeComponentStore",
    ),
    description=(
        "Copy this command into an administrator terminal if you want an official component "
        "store analysis. Reclaimer does not run or audit it."
    ),
    requires_administrator=True,
    official_source=(
        "https://learn.microsoft.com/windows-hardware/manufacture/desktop/"
        "determine-the-actual-size-of-the-winsxs-folder"
    ),
)


def report_only_resources() -> tuple[Resource, ...]:
    """Return non-actionable resources that carry official external guidance."""

    guide = COMPONENT_STORE_ANALYSIS
    return (
        Resource(
            candidate_id=new_id("candidate"),
            adapter_id="windows_maintenance",
            display_name=guide.title,
            semantic_type=SemanticType.SYSTEM_ROLLBACK,
            risk_tier=RiskTier.RED,
            provenance_class=ProvenanceClass.REGENERABLE_CONFIRMED,
            undo_capability=UndoCapability.NONE,
            reconstruction=Reconstruction.NONE,
            warnings=(
                "REPORT_ONLY: Reclaimer will not execute or audit this administrator command.",
                f"External command: {guide.display_command}",
            ),
            evidence=(
                Evidence(
                    source=guide.official_source,
                    detail=guide.description,
                    checked_at=datetime(2026, 7, 10, tzinfo=UTC),
                ),
            ),
            actionable=False,
        ),
    )

