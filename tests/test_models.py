from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from reclaimer.core.models import (
    Action,
    ActionKind,
    Confidence,
    EffectClass,
    Evidence,
    FileIdentity,
    Plan,
    PreviewMode,
    ProvenanceClass,
    ReclaimScope,
    Reconstruction,
    Resource,
    RiskTier,
    SelectionMode,
    SemanticType,
    SizeValue,
    UndoCapability,
    new_id,
)
from reclaimer.core.policy import build_inventory_plan


def test_size_value_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        SizeValue(-1, Confidence.EXACT)


@pytest.mark.parametrize("value", (True, 1.5, "1"))
def test_size_value_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        SizeValue(value, Confidence.EXACT)


def test_missing_size_requires_unknown_confidence() -> None:
    with pytest.raises(ValueError):
        SizeValue(None, Confidence.ESTIMATE)


def test_unknown_resource_is_red_and_not_actionable() -> None:
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Unknown directory",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
    )
    assert resource.actionable is False


def test_local_only_resource_cannot_be_actionable() -> None:
    with pytest.raises(ValueError):
        Resource(
            candidate_id=new_id("candidate"),
            adapter_id="ollama",
            display_name="Local model",
            semantic_type=SemanticType.INSTALLED_MODEL,
            risk_tier=RiskTier.YELLOW,
            provenance_class=ProvenanceClass.LOCAL_ONLY,
            actionable=True,
        )


def test_inventory_plan_contains_no_enabled_actions() -> None:
    resource = Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Report item",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        undo_capability=UndoCapability.NONE,
        reconstruction=Reconstruction.NONE,
        evidence=(
            Evidence(
                source="test",
                detail="fixture",
                checked_at=datetime.now(UTC),
            ),
        ),
    )
    plan = build_inventory_plan("scan_test", [resource])
    assert plan.is_inventory_only
    assert all(not action.enabled for action in plan.actions)
    assert plan.to_dict()["executable"] is False


def test_report_only_action_rejects_executable_semantics() -> None:
    with pytest.raises(ValueError, match="non-executable semantics"):
        Action(
            action_id=new_id("action"),
            candidate_id=new_id("candidate"),
            adapter_id="filesystem",
            kind=ActionKind.REPORT_ONLY,
            effect_class=EffectClass.MAINTENANCE,
            selection_mode=SelectionMode.NONE,
            preview_mode=PreviewMode.NONE,
            reclaim_scope=ReclaimScope.UNKNOWN,
            risk_tier=RiskTier.RED,
            summary="invalid fixture",
        )


def test_inventory_plan_rejects_duplicate_candidate_selection() -> None:
    candidate_id = new_id("candidate")
    resources = [
        Resource(
            candidate_id=candidate_id,
            adapter_id="filesystem",
            display_name=f"Duplicate {index}",
            semantic_type=SemanticType.UNKNOWN,
            risk_tier=RiskTier.RED,
            provenance_class=ProvenanceClass.UNKNOWN,
        )
        for index in range(2)
    ]

    with pytest.raises(ValueError, match="unique"):
        build_inventory_plan("scan_test", resources)


def test_ids_are_opaque_and_prefixed() -> None:
    first = new_id("scan")
    second = new_id("scan")
    assert first.startswith("scan_")
    assert first != second


def _resource_fixture() -> Resource:
    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Fixture",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
    )


def _action_fixture() -> Action:
    return Action(
        action_id=new_id("action"),
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        kind=ActionKind.REPORT_ONLY,
        effect_class=EffectClass.PURE_QUERY,
        selection_mode=SelectionMode.NONE,
        preview_mode=PreviewMode.NONE,
        reclaim_scope=ReclaimScope.UNKNOWN,
        risk_tier=RiskTier.RED,
        summary="Report only fixture",
    )


def _plan_fixture() -> Plan:
    created_at = datetime.now(UTC)
    return Plan(
        plan_id=new_id("plan"),
        scan_id=new_id("scan"),
        schema_version="1.0.0",
        engine_build_id="0.1.0",
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
        actions=(_action_fixture(),),
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("volume_serial", "v" * 129),
        ("file_id", "f" * 257),
        ("file_id_kind", "k" * 65),
        ("link_count", -1),
        ("attributes", True),
        ("reparse_tag", -1),
        ("creation_time_ns", -1),
        ("last_write_time_ns", -1),
    ),
)
def test_file_identity_enforces_schema_bounds(field_name: str, value: object) -> None:
    with pytest.raises(ValueError, match=field_name):
        FileIdentity(**{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("source", ""),
        ("source", "s" * 1025),
        ("detail", ""),
        ("detail", "d" * 8193),
        ("digest", "a" * 257),
    ),
)
def test_evidence_enforces_schema_string_bounds(field_name: str, value: str) -> None:
    values = {
        "source": "fixture",
        "detail": "bounded evidence",
        "checked_at": datetime.now(UTC),
        "digest": None,
    }
    values[field_name] = value
    with pytest.raises(ValueError, match=field_name):
        Evidence(**values)


def test_evidence_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="checked_at"):
        Evidence(source="fixture", detail="evidence", checked_at=datetime.now())


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("candidate_id", "bad/id"),
        ("candidate_id", "c" * 257),
        ("adapter_id", "FileSystem"),
        ("adapter_id", "a" * 65),
        ("display_name", ""),
        ("display_name", "d" * 513),
        ("vendor_locator", "v" * 32768),
        ("path", "p" * 32768),
        ("reconstruction_preconditions", ("",)),
        ("reconstruction_preconditions", ("p" * 1025,)),
        ("reconstruction_preconditions", ("p",) * 65),
        ("warnings", ("",)),
        ("warnings", ("w" * 2049,)),
        ("warnings", ("w",) * 257),
    ),
    ids=(
        "candidate-format",
        "candidate-length",
        "adapter-format",
        "adapter-length",
        "display-empty",
        "display-length",
        "vendor-locator-length",
        "path-length",
        "precondition-empty",
        "precondition-length",
        "precondition-count",
        "warning-empty",
        "warning-length",
        "warning-count",
    ),
)
def test_resource_enforces_schema_string_and_array_bounds(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(_resource_fixture(), **{field_name: value})


def test_resource_enforces_evidence_count_limit() -> None:
    evidence = Evidence(source="fixture", detail="evidence", checked_at=datetime.now(UTC))
    with pytest.raises(ValueError, match="at most 256"):
        replace(_resource_fixture(), evidence=(evidence,) * 257)


def test_unknown_resource_cannot_be_actionable_with_verified_provenance() -> None:
    with pytest.raises(ValueError, match="UNKNOWN resources cannot be actionable"):
        replace(
            _resource_fixture(),
            provenance_class=ProvenanceClass.REGENERABLE_CONFIRMED,
            actionable=True,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("action_id", "action_" + "g" * 32),
        ("candidate_id", "bad/candidate"),
        ("adapter_id", "BadAdapter"),
        ("summary", ""),
        ("summary", "s" * 4097),
        ("enabled", "false"),
    ),
)
def test_action_enforces_inventory_plan_schema_bounds(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(_action_fixture(), **{field_name: value})


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("plan_id", "plan_invalid"),
        ("scan_id", "bad/scan"),
        ("engine_build_id", ""),
        ("engine_build_id", "e" * 129),
    ),
)
def test_plan_enforces_identifier_and_build_bounds(field_name: str, value: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        replace(_plan_fixture(), **{field_name: value})


def test_plan_enforces_action_count_limit() -> None:
    with pytest.raises(ValueError, match="at most 256"):
        replace(_plan_fixture(), actions=(_action_fixture(),) * 257)


def test_plan_rejects_future_action_shapes_in_inventory_milestone() -> None:
    future_action = replace(
        _action_fixture(),
        kind=ActionKind.EXACT_VENDOR_ACTION,
        effect_class=EffectClass.MAINTENANCE,
        selection_mode=SelectionMode.EXACT_IDS,
        preview_mode=PreviewMode.VENDOR_EXACT,
        reclaim_scope=ReclaimScope.VENDOR_LOGICAL,
        enabled=True,
    )

    with pytest.raises(ValueError, match="only accepts REPORT_ONLY"):
        replace(_plan_fixture(), actions=(future_action,))


@pytest.mark.parametrize("prefix", ("", "Bad", "bad-prefix", "p" * 65))
def test_new_id_rejects_prefixes_outside_the_schema_safe_subset(prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        new_id(prefix)
