"""Bounded aggregate measurements for adapter-owned local roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from devclean.adapters.base import AdapterIssue
from devclean.core.models import Confidence, SizeValue
from devclean.scanner import CancellationToken, ScanOptions, ScanStats, scan_roots


@dataclass(frozen=True, slots=True)
class TreeMeasurement:
    logical_size: SizeValue
    allocated_size: SizeValue
    files: int
    boundaries: int
    inaccessible: int
    issues: tuple[AdapterIssue, ...]


def measure_tree(
    root: Path, *, cancel: CancellationToken | None = None
) -> TreeMeasurement:
    """Measure an approved root without retaining per-file records in memory."""

    last = ScanStats()

    def progress(stats: ScanStats) -> None:
        nonlocal last
        last = stats

    for _ in scan_roots(
        (root,),
        ScanOptions(include_directories=False),
        cancel=cancel,
        progress=progress,
    ):
        pass

    incomplete = bool(
        last.errors
        or last.boundaries
        or last.allocation_unknown_files
        or last.cancelled
    )
    confidence = Confidence.ESTIMATE if incomplete else Confidence.EXACT
    issues: list[AdapterIssue] = []
    if last.cancelled:
        issues.append(AdapterIssue("SCAN_CANCELLED", "Root measurement was cancelled.", True))
    if last.errors:
        issues.append(
            AdapterIssue(
                "FILESYSTEM_ERRORS",
                f"Root measurement skipped {last.errors} inaccessible or failed observations.",
            )
        )
    if last.boundaries:
        issues.append(
            AdapterIssue(
                "FILESYSTEM_BOUNDARIES",
                f"Root measurement did not traverse {last.boundaries} reparse/cloud boundaries.",
            )
        )
    if last.allocation_unknown_files:
        issues.append(
            AdapterIssue(
                "ALLOCATION_UNKNOWN",
                "Some hard-link allocation contributions were unknown; the total is a lower "
                "bound.",
            )
        )
    return TreeMeasurement(
        logical_size=SizeValue(last.logical_bytes, confidence),
        allocated_size=SizeValue(last.allocated_bytes, confidence),
        files=last.files,
        boundaries=last.boundaries,
        inaccessible=last.inaccessible,
        issues=tuple(issues),
    )


__all__ = ["TreeMeasurement", "measure_tree"]
