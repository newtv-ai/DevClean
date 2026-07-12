from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing import Resource as SchemaResource

from reclaimer.core.models import (
    Confidence,
    Evidence,
    ProvenanceClass,
    Resource,
    RiskTier,
    SemanticType,
    SizeValue,
    new_id,
)
from reclaimer.core.policy import build_inventory_plan

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_SCHEMA_PATH = ROOT / "schemas" / "resource.schema.json"
SCAN_REPORT_SCHEMA_PATH = ROOT / "schemas" / "scan-report.schema.json"
INVENTORY_PLAN_SCHEMA_PATH = ROOT / "schemas" / "inventory-plan.schema.json"
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

REQUIRED_DOCS = (
    ROOT / "docs" / "adr" / "ADR-001-independent-engine.md",
    ROOT / "docs" / "adr" / "ADR-002-ai-excluded.md",
    ROOT / "docs" / "adr" / "ADR-003-third-party-license-boundary.md",
    ROOT / "docs" / "threat-model.md",
    ROOT / "docs" / "coverage-matrix.md",
    ROOT / "docs" / "adapter-support.md",
    ROOT / "docs" / "evidence" / "README.md",
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_local_markdown_links_resolve_inside_the_repository() -> None:
    documents = (ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")))
    missing: list[str] = []
    for document in documents:
        for raw_target in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or target.startswith(("https://", "http://", "mailto:")):
                continue
            resolved = (document.parent / target).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                missing.append(f"{document.relative_to(ROOT)} -> escapes repository: {target}")
                continue
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> missing: {target}")
    assert missing == []


def _validators() -> tuple[Draft202012Validator, Draft202012Validator]:
    resource_schema = _load_json(RESOURCE_SCHEMA_PATH)
    report_schema = _load_json(SCAN_REPORT_SCHEMA_PATH)
    Draft202012Validator.check_schema(resource_schema)
    Draft202012Validator.check_schema(report_schema)
    registry = Registry().with_resource(
        resource_schema["$id"], SchemaResource.from_contents(resource_schema)
    )
    format_checker = FormatChecker()
    return (
        Draft202012Validator(resource_schema, format_checker=format_checker),
        Draft202012Validator(
            report_schema,
            registry=registry,
            format_checker=format_checker,
        ),
    )


def _resource_model() -> Resource:
    return Resource(
        candidate_id=new_id("candidate"),
        adapter_id="filesystem",
        display_name="Unknown directory",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=r"C:\Users\<USER>\workspace",
        logical_size=SizeValue(4096, Confidence.EXACT),
        allocated_size=SizeValue(8192, Confidence.EXACT),
        evidence=(
            Evidence(
                source="filesystem",
                detail="metadata-only fixture",
                checked_at=datetime.now(UTC),
            ),
        ),
    )


def _resource() -> dict[str, Any]:
    return _resource_model().to_dict()


def _scan_report(resource: dict[str, Any]) -> dict[str, Any]:
    timestamp = datetime.now(UTC).isoformat()
    return {
        "schema_version": "1.0.0",
        "generated_at": timestamp,
        "scan": {
            "scan_id": new_id("scan"),
            "schema_version": "1.0.0",
            "engine_version": "0.0.1",
            "status": "COMPLETED",
            "started_at": timestamp,
            "finished_at": timestamp,
            "roots": [r"C:\Users\<USER>\workspace"],
            "summary": {"files": 1},
        },
        "resources": [resource],
        "errors": [],
        "adapter_runs": [],
        "evidence": [],
        "safety_boundary": {
            "executable": False,
            "statement": "Inventory only; this report cannot be imported or executed.",
        },
    }


def test_required_phase_zero_documents_exist_and_are_not_placeholders() -> None:
    for path in REQUIRED_DOCS:
        assert path.is_file(), f"missing baseline document: {path.relative_to(ROOT)}"
        content = path.read_text(encoding="utf-8")
        assert len(content) >= 300, f"baseline document is unexpectedly short: {path}"
        assert "TODO" not in content


def test_adrs_record_the_non_executable_boundaries() -> None:
    adr_001 = REQUIRED_DOCS[0].read_text(encoding="utf-8")
    adr_002 = REQUIRED_DOCS[1].read_text(encoding="utf-8")
    adr_003 = REQUIRED_DOCS[2].read_text(encoding="utf-8")
    assert "Accepted" in adr_001 and "preview" in adr_001 and "--clean" in adr_001
    assert "Accepted" in adr_002 and "ai_feedback" in adr_002
    assert "Accepted" in adr_003 and "Winapp2.ini" in adr_003


def test_adapter_baseline_classifies_commands_with_known_side_effects() -> None:
    content = (ROOT / "docs" / "adapter-support.md").read_text(encoding="utf-8")
    assert "npm cache verify" in content
    assert "execute" in content
    assert "cache, rm, <revision_hash>" in content
    assert "--force-pkgs-dirs" in content
    assert "repo@rev" in content
    assert "vendor logical" in content
    assert "host physical" in content


def test_resource_and_scan_report_samples_validate_without_network_resolution() -> None:
    resource_validator, report_validator = _validators()
    resource = _resource()
    resource_validator.validate(resource)
    report_validator.validate(_scan_report(resource))


def test_completed_adapter_run_contract_validates() -> None:
    _, report_validator = _validators()
    timestamp = datetime.now(UTC).isoformat()
    report = _scan_report(_resource())
    report["adapter_runs"] = [
        {
            "run_id": new_id("adapter_run"),
            "scan_id": report["scan"]["scan_id"],
            "adapter_id": "pip",
            "status": "AVAILABLE",
            "version": "26.1.1",
            "effect_class": "PURE_QUERY",
            "started_at": timestamp,
            "finished_at": timestamp,
            "completed": True,
            "executable": r"C:\Python\python.exe",
            "detail": "fixture",
            "resources": 1,
            "issues": [],
            "evidence_ids": [],
        }
    ]

    report_validator.validate(report)


def test_inventory_plan_schema_requires_report_only_disabled_actions() -> None:
    schema = _load_json(INVENTORY_PLAN_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    payload = build_inventory_plan("scan_fixture", [_resource_model()]).to_dict()
    validator.validate(payload)

    payload["actions"][0]["enabled"] = True
    with pytest.raises(ValidationError):
        validator.validate(payload)


def test_unknown_or_local_only_resources_cannot_be_actionable() -> None:
    resource_validator, _ = _validators()
    resource = _resource()
    resource["actionable"] = True
    with pytest.raises(ValidationError):
        resource_validator.validate(resource)


def test_inventory_scan_report_rejects_otherwise_valid_actionable_resource() -> None:
    resource_validator, report_validator = _validators()
    resource = deepcopy(_resource())
    resource.update(
        {
            "semantic_type": "REBUILDABLE_CACHE",
            "risk_tier": "GREEN",
            "provenance_class": "REGENERABLE_CONFIRMED",
            "actionable": True,
        }
    )
    resource_validator.validate(resource)
    with pytest.raises(ValidationError):
        report_validator.validate(_scan_report(resource))


def test_missing_size_requires_unknown_confidence_in_schema() -> None:
    resource_validator, _ = _validators()
    resource = _resource()
    resource["exclusive_host_reclaimable"] = {
        "value": None,
        "confidence": "ESTIMATE",
    }
    with pytest.raises(ValidationError):
        resource_validator.validate(resource)


def test_windows_ci_covers_supported_python_versions() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "windows-latest" in workflow
    for version in ("3.11", "3.12", "3.13"):
        assert version in workflow
    assert 'version: "0.11.6"' in workflow
    assert "uv lock --check" in workflow
    assert "uv sync --frozen" in workflow
    assert "scripts/validate_schemas.py" in workflow
    assert "uv run --frozen pytest" in workflow
    assert "uv run --frozen ruff check" in workflow
    assert "uv run --frozen mypy src" in workflow
    assert "python -m pip_audit" in workflow
    assert "--format json" in workflow
    assert "--junitxml=" in workflow
    assert "artifacts/ci/dependency-audit" in workflow
    assert "scripts/build_release.ps1" in workflow
    assert "artifacts/release/*" in workflow


def test_codeql_security_analysis_is_configured_for_python() -> None:
    workflow = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(
        encoding="utf-8"
    )
    assert "github/codeql-action/init@" in workflow
    assert "github/codeql-action/analyze@" in workflow
    assert "languages: python" in workflow
    assert "build-mode: none" in workflow
    assert "actions: read" in workflow


def test_all_github_actions_are_pinned_to_full_commit_shas() -> None:
    workflows = tuple((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    use_pattern = re.compile(r"^\s*uses:\s*[^@\s]+@([^\s#]+)", re.MULTILINE)
    for path in workflows:
        content = path.read_text(encoding="utf-8")
        references = use_pattern.findall(content)
        assert references, f"workflow has no action references: {path.name}"
        assert all(re.fullmatch(r"[a-f0-9]{40}", ref) for ref in references), path.name
        assert "persist-credentials: false" in content, path.name


def test_retained_million_row_benchmark_is_bounded_and_explicitly_local() -> None:
    path = (
        ROOT
        / "docs"
        / "evidence"
        / "benchmarks"
        / "2026-07-12-streaming-million.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "1.0.0"
    assert payload["evidence_kind"] == "G1_STREAMING_STATE_BENCHMARK"
    assert re.fullmatch(r"[a-f0-9]{64}", payload["artifact_sha256"])
    assert payload["source_revision"] == "WORKTREE_UNCOMMITTED"
    assert payload["result"]["count"] == 1_000_000
    assert payload["result"]["stored"] == 1_000_000
    assert payload["result"]["batch_size"] == 512
    assert payload["result"]["integrity"] is True
    assert payload["result"]["peak_python_traced_bytes"] < 64 * 1024 * 1024
    assert payload["result"]["peak_working_set_bytes"] < 512 * 1024 * 1024
    assert payload["verification"] == {
        "bounded_batching": True,
        "row_count_matches": True,
        "sqlite_integrity_check": "ok",
    }
