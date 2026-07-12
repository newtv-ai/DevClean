from dataclasses import replace

import pytest

from reclaimer.adapters.base import InventoryResult, ProbeResult, ProbeStatus
from reclaimer.adapters.catalog import get_descriptor, list_descriptors
from reclaimer.adapters.windows_maintenance import (
    COMPONENT_STORE_ANALYSIS,
    report_only_resources,
)
from reclaimer.core.models import (
    EffectClass,
    ProvenanceClass,
    Resource,
    RiskTier,
    SemanticType,
    new_id,
)


def test_catalog_ids_are_unique_and_inventory_only() -> None:
    descriptors = list_descriptors()
    ids = [descriptor.adapter_id for descriptor in descriptors]
    assert len(ids) == len(set(ids))
    assert all(descriptor.inventory_only for descriptor in descriptors)


def test_catalog_is_explicit() -> None:
    descriptor = get_descriptor("huggingface")
    assert descriptor is not None
    assert get_descriptor("third_party_plugin") is None


def test_catalog_effects_match_current_inventory_implementations() -> None:
    effects = {item.adapter_id: item.effect_class for item in list_descriptors()}
    assert effects["conda"] is EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES
    assert effects["docker"] is EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES
    assert effects["pnpm"] is EffectClass.PURE_QUERY
    assert effects["vscode"] is EffectClass.PURE_QUERY


def test_windows_maintenance_is_report_only() -> None:
    resources = report_only_resources()
    assert len(resources) == 1
    assert resources[0].semantic_type is SemanticType.SYSTEM_ROLLBACK
    assert resources[0].actionable is False
    assert "DISM.exe" in resources[0].warnings[1]
    assert COMPONENT_STORE_ANALYSIS.externally_executed


def test_inventory_result_rejects_actionable_resource() -> None:
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="fixture",
        display_name="Future executable candidate",
        semantic_type=SemanticType.REBUILDABLE_CACHE,
        risk_tier=RiskTier.GREEN,
        provenance_class=ProvenanceClass.REGENERABLE_CONFIRMED,
    )
    resource = replace(resource, actionable=True)

    with pytest.raises(ValueError, match="cannot emit actionable"):
        InventoryResult(
            adapter_id="fixture",
            probe=ProbeResult("fixture", ProbeStatus.AVAILABLE),
            resources=(resource,),
        )
