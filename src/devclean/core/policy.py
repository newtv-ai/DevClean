"""Fail-closed policy for the inventory-only milestone."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from devclean import __version__
from devclean.core.models import (
    SCHEMA_VERSION,
    Action,
    ActionKind,
    EffectClass,
    Plan,
    PreviewMode,
    ReclaimScope,
    Reconstruction,
    Resource,
    RiskTier,
    SelectionMode,
    UndoCapability,
    new_id,
    utc_now,
)


def build_inventory_plan(scan_id: str, resources: Iterable[Resource]) -> Plan:
    """Build a non-executable plan for review and schema testing.

    This deliberately emits only REPORT_ONLY actions. Future milestones must add new builders
    rather than weakening this function.
    """

    return _build_inventory_plan(
        scan_id,
        (
            _InventorySelection(
                candidate_id=resource.candidate_id,
                adapter_id=resource.adapter_id,
                display_name=resource.display_name,
                risk_tier=resource.risk_tier,
                undo_capability=resource.undo_capability,
                reconstruction=resource.reconstruction,
            )
            for resource in resources
        ),
    )


def build_inventory_plan_from_records(
    scan_id: str, records: Iterable[Mapping[str, Any]]
) -> Plan:
    """Build a report-only plan from strict records loaded from the local state DB."""

    selections: list[_InventorySelection] = []
    for record in records:
        candidate_id = record.get("candidate_id")
        adapter_id = record.get("adapter_id")
        display_name = record.get("display_name")
        risk_tier = record.get("risk_tier")
        undo_capability = record.get("undo_capability")
        reconstruction = record.get("reconstruction")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(adapter_id, str)
            or not adapter_id
            or not isinstance(display_name, str)
            or not display_name
            or len(display_name) > 2048
            or not isinstance(risk_tier, str)
            or not isinstance(undo_capability, str)
            or not isinstance(reconstruction, str)
            or record.get("actionable") is not False
        ):
            raise ValueError("stored resource is invalid for an inventory-only plan")
        try:
            selection = _InventorySelection(
                candidate_id=candidate_id,
                adapter_id=adapter_id,
                display_name=display_name,
                risk_tier=RiskTier(risk_tier),
                undo_capability=UndoCapability(undo_capability),
                reconstruction=Reconstruction(reconstruction),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("stored resource has invalid plan semantics") from error
        selections.append(selection)
    return _build_inventory_plan(scan_id, selections)


@dataclass(frozen=True, slots=True)
class _InventorySelection:
    candidate_id: str
    adapter_id: str
    display_name: str
    risk_tier: RiskTier
    undo_capability: UndoCapability
    reconstruction: Reconstruction


def _build_inventory_plan(
    scan_id: str, selections: Iterable[_InventorySelection]
) -> Plan:
    created_at = utc_now()
    actions = tuple(
        Action(
            action_id=new_id("action"),
            candidate_id=selection.candidate_id,
            adapter_id=selection.adapter_id,
            kind=ActionKind.REPORT_ONLY,
            effect_class=EffectClass.PURE_QUERY,
            selection_mode=SelectionMode.NONE,
            preview_mode=PreviewMode.NONE,
            reclaim_scope=ReclaimScope.UNKNOWN,
            risk_tier=selection.risk_tier,
            summary=f"Report only: {selection.display_name}",
            undo_capability=selection.undo_capability,
            reconstruction=selection.reconstruction,
            enabled=False,
        )
        for selection in selections
    )
    return Plan(
        plan_id=new_id("plan"),
        scan_id=scan_id,
        schema_version=SCHEMA_VERSION,
        engine_build_id=__version__,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
        actions=actions,
    )
