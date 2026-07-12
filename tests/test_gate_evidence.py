from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_script("validate_gate_evidence")
PROCMON = _load_script("validate_procmon_csv")
validate_manifest = cast(Callable[[Path, Draft202012Validator], Any], GATE.validate_manifest)
validate_matrix = cast(Callable[[str, list[Any]], list[str]], GATE.validate_matrix)
SCHEMA_VALIDATOR = GATE._schema_validator(ROOT / "schemas" / "gate-evidence.schema.json")

NOW = "2026-07-11T00:00:00+00:00"
REVISION = "abcdef1234567890abcdef1234567890abcdef12"
G1_CHECKS = set(GATE.G1_REQUIRED_CHECKS)
G0_CHECKS = set(GATE.G0_REQUIRED_CHECKS)
G1_BOUNDARY_CHECKS = set(GATE.G1_BOUNDARY_CHECKS)
G1_TEST_CHECKS = set(GATE.G1_TEST_CHECKS)
G2_CHECKS = set(GATE.G2_REQUIRED_CHECKS)
G2_ADAPTERS = set(GATE.G2_ADAPTERS)
G5_CHECKS = set(GATE.G5_REQUIRED_CHECKS)
G5_RACES = set(GATE.G5_RACE_CHECKS)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_artifact(
    directory: Path,
    manifest: dict[str, Any],
    *,
    evidence_id: str,
    kind: str,
    filename: str,
    content: bytes,
    sensitive: bool = False,
) -> str:
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    manifest["evidence"].append(
        {
            "evidence_id": evidence_id,
            "kind": kind,
            "relative_path": filename.replace("\\", "/"),
            "sha256": _sha(content),
            "bytes": len(content),
            "contains_sensitive_data": sensitive,
            "description": f"Synthetic {kind} fixture",
        }
    )
    return evidence_id


def _environment(
    *, machine_kind: str = "PHYSICAL", fingerprint: str = "machine-a"
) -> dict[str, Any]:
    return {
        "machine_kind": machine_kind,
        "machine_fingerprint_sha256": _sha(fingerprint.encode()),
        "os_product": "Windows 11 Pro",
        "os_version": "10.0.26200",
        "os_build": "26200.1000",
        "architecture": "x86_64",
        "locale": "en-US",
        "filesystem": "NTFS",
        "volume_kind": "FIXED",
        "user_integrity": "STANDARD",
        "isolation_notes": "Disposable synthetic fixture; no user data.",
    }


def _base_manifest(gate: str, product_hash: str) -> dict[str, Any]:
    return {
        "$schema": "../../../schemas/gate-evidence.schema.json",
        "schema_version": "1.0.0",
        "gate": gate,
        "run_id": f"gate_run_{gate.casefold()}_fixture01",
        "captured_at": NOW,
        "product": {
            "version": "0.1.0",
            "source_revision": REVISION,
            "artifact_kind": "WHEEL",
            "artifact_sha256": product_hash,
        },
        "prerequisites": [],
        "environment": _environment(),
        "scope": {
            "adapters": ["filesystem"],
            "available_adapters": ["filesystem"],
            "required_process_names": ["python.exe"],
            "managed_root_labels": ["managed_cache"],
            "protected_asset_labels": ["user_assets"],
            "allowed_write_root_labels": ["reclaimer_data"],
        },
        "evidence": [],
        "checks": [],
        "review": {
            "author_ids": ["author_fixture"],
            "reviewer_ids": [],
            "review_method": "NONE",
            "reviewed_at": None,
            "notes": "Synthetic validator fixture.",
        },
        "limitations": ["Synthetic test evidence only."],
        "conclusion": "PASS",
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def _build_g1(directory: Path) -> tuple[Path, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    product_content = b"synthetic-reclaimer-wheel"
    product_hash = _sha(product_content)
    manifest = _base_manifest("G1", product_hash)
    product_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_product_fixture01",
        kind="PRODUCT_ARTIFACT",
        filename="reclaimer.whl",
        content=product_content,
    )
    test_report = {
        "schema_version": "1.0.0",
        "artifact_sha256": product_hash,
        "source_revision": REVISION,
        "started_at": NOW,
        "finished_at": "2026-07-11T00:01:00+00:00",
        "command": ["python.exe", "-m", "pytest", "tests/fs_integration"],
        "exit_code": 0,
        "checks": [
            {
                "check_id": check_id,
                "status": "PASS",
                "duration_ms": 1,
                "detail": "Synthetic closed-contract success.",
            }
            for check_id in sorted(G1_TEST_CHECKS)
        ],
    }
    test_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_test_report_fixture01",
        kind="TEST_REPORT_JSON",
        filename="test-report.json",
        content=_json_bytes(test_report),
    )
    observations = []
    for check_id in sorted(G1_BOUNDARY_CHECKS):
        digest = _sha(check_id.encode())
        observations.append(
            {
                "check_id": check_id,
                "fixture_label": f"fixture_{check_id}",
                "boundary_reason": (
                    "CLOUD_FILES_PLACEHOLDER"
                    if check_id == "onedrive_placeholder_not_hydrated"
                    else "INVENTORY_ONLY"
                    if check_id
                    in {
                        "unc_no_execution_candidate",
                        "network_volume_no_execution_candidate",
                        "removable_volume_no_execution_candidate",
                    }
                    else "ACCESS_DENIED"
                    if check_id == "access_denied_no_uac"
                    else "REPARSE_POINT"
                ),
                "descended": False,
                "hydrated": False,
                "actionable_resources": 0,
                "before_identity_digest": digest,
                "after_identity_digest": digest,
            }
        )
    boundary_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_boundary_fixture01",
        kind="BOUNDARY_OBSERVATION_JSON",
        filename="boundary.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "captured_at": NOW,
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "observations": observations,
            }
        ),
    )
    benchmark_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_benchmark_fixture01",
        kind="BENCHMARK_JSON",
        filename="benchmark.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "evidence_kind": "G1_STREAMING_STATE_BENCHMARK",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "captured_at": NOW,
                "result": {
                    "batch_size": 512,
                    "count": 1_000_000,
                    "stored": 1_000_000,
                    "database_bytes": 1,
                    "peak_python_traced_bytes": 2_000_000,
                    "peak_working_set_bytes": 30_000_000,
                    "integrity": True,
                },
                "verification": {
                    "bounded_batching": True,
                    "row_count_matches": True,
                    "sqlite_integrity_check": "ok",
                },
            }
        ),
    )
    for check_id in sorted(G1_CHECKS):
        refs = [benchmark_id] if check_id == "million_resource_streaming" else [test_id]
        if check_id in G1_BOUNDARY_CHECKS:
            refs.append(boundary_id)
        manifest["checks"].append(
            {
                "check_id": check_id,
                "status": "PASS",
                "required": True,
                "evidence_refs": refs,
                "explanation": "Synthetic test fixture.",
            }
        )
    assert product_id
    manifest_path = directory / "g1-manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest_path, manifest


def _build_g0(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    product_content = b"synthetic-reclaimer-wheel"
    product_hash = _sha(product_content)
    manifest = _base_manifest("G0", product_hash)
    manifest["environment"]["machine_kind"] = "CI_RUNNER"
    manifest["environment"]["user_integrity"] = "ELEVATED"
    manifest["scope"] = {
        "adapters": ["repository"],
        "available_adapters": ["repository"],
        "required_process_names": ["python.exe"],
        "managed_root_labels": [],
        "protected_asset_labels": [],
        "allowed_write_root_labels": [],
    }
    manifest["review"]["author_ids"] = ["owner_fixture"]
    _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_product_fixture00",
        kind="PRODUCT_ARTIFACT",
        filename="reclaimer.whl",
        content=product_content,
    )
    owner_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_owner_fixture00",
        kind="OWNER_ATTESTATION_JSON",
        filename="owner.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "source_revision": REVISION,
                "decision_at": NOW,
                "owner_id": "owner_fixture",
                "license_expression": "GPL-3.0-or-later",
                "approved": True,
                "no_third_party_rule_copy_attested": True,
                "notes": "Synthetic owner decision fixture.",
            }
        ),
    )
    digest = _sha(b"source-boundary")
    source_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_source_audit_fixture00",
        kind="SOURCE_AUDIT_JSON",
        filename="source-audit.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "evidence_kind": "G0_SOURCE_BOUNDARY_AUDIT",
                "captured_at": NOW,
                "source_revision": REVISION,
                "source_tree_sha256": digest,
                "auditor_sha256": _sha(b"auditor"),
                "checked_file_count": 100,
                "checked_total_bytes": 100_000,
                "license_sha256": (
                    "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
                ),
                "third_party_notices_sha256": _sha(b"notices"),
                "pyproject_sha256": _sha(b"project"),
                "runtime_dependencies": [],
                "declared_license_expression": "GPL-3.0-or-later",
                "declared_license_files": ["LICENSE", "THIRD_PARTY_NOTICES.md"],
                "console_scripts": {"reclaimer": "reclaimer.cli.main:main"},
                "runtime_plugin_groups": [],
                "prohibited_vendored_paths": [],
                "mechanical_result": "PASS",
                "owner_license_decision_proven": False,
                "originality_proven": False,
                "limitations": ["Synthetic mechanical fixture."],
            }
        ),
    )
    dependency_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_dependency_audit_fixture00",
        kind="DEPENDENCY_AUDIT_JSON",
        filename="dependency-audit.json",
        content=_json_bytes(
            {
                "dependencies": [{"name": "pip", "version": "1.0", "vulns": []}],
                "fixes": [],
            }
        ),
    )
    release_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_release_validation_fixture00",
        kind="RELEASE_VALIDATION_JSON",
        filename="release-validation.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "source_revision": REVISION,
                "version": "0.1.0",
                "captured_at": NOW,
                "artifact_sha256": product_hash,
                "wheel_sha256": product_hash,
                "sbom_sha256": _sha(b"sbom"),
                "checksums_sha256": _sha(b"checksums"),
                "builder_sha256": _sha(b"builder"),
                "validator_sha256": _sha(b"validator"),
                "uv_lock_sha256": _sha(b"lock"),
                "clean_runtime_install": True,
                "wheel_reproducible": True,
                "sbom_reproducible": True,
                "schemas_validated": True,
                "wheel_record_validated": True,
                "inventory_only_surface_validated": True,
                "result": "PASS",
            }
        ),
    )
    evidence_by_id = {entry["evidence_id"]: entry for entry in manifest["evidence"]}
    ci_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_ci_attestation_fixture00",
        kind="CI_ATTESTATION_JSON",
        filename="ci.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "source_revision": REVISION,
                "artifact_sha256": product_hash,
                "captured_at": NOW,
                "workflow_sha256": _sha(b"ci-workflow"),
                "run_id": "123456",
                "run_attempt": 1,
                "repository": "example/reclaimer",
                "python_matrix": [
                    {
                        "python_version": version,
                        "conclusion": "PASS",
                        "tests_passed": 200,
                        "tests_skipped": 2,
                        "coverage_percent": 80.0,
                        "schemas_validated": True,
                        "dependency_audit_clean": True,
                        "ruff_passed": True,
                        "mypy_passed": True,
                    }
                    for version in ("3.11", "3.12", "3.13")
                ],
                "actions_pinned": True,
                "persist_credentials_false": True,
                "inventory_only_contract_tests_passed": True,
                "dependency_audit_sha256": evidence_by_id[dependency_id]["sha256"],
                "release_validation_sha256": evidence_by_id[release_id]["sha256"],
                "conclusion": "PASS",
            }
        ),
    )
    codeql_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_codeql_fixture00",
        kind="CODEQL_ATTESTATION_JSON",
        filename="codeql.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "source_revision": REVISION,
                "captured_at": NOW,
                "workflow_sha256": _sha(b"codeql-workflow"),
                "run_id": "654321",
                "language": "python",
                "build_mode": "none",
                "query_suite": "security-extended",
                "unresolved_error_alerts": 0,
                "conclusion": "PASS",
            }
        ),
    )
    refs = {
        "license_owner_confirmed": [owner_id],
        "third_party_boundary_clean": [source_id],
        "windows_ci_matrix_passed": [ci_id],
        "codeql_passed": [codeql_id],
        "dependency_audit_clean": [dependency_id],
        "release_artifacts_validated": [release_id],
        "preview_inventory_only": [ci_id, release_id],
    }
    for check_id in sorted(G0_CHECKS):
        manifest["checks"].append(
            {
                "check_id": check_id,
                "status": "PASS",
                "required": True,
                "evidence_refs": refs[check_id],
                "explanation": "Synthetic G0 contract fixture.",
            }
        )
    manifest_path = directory / "g0-manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest_path


def _scan_report() -> dict[str, Any]:
    scan_id = "scan_fixture"
    runs = []
    for index, adapter in enumerate(sorted(G2_ADAPTERS), start=1):
        runs.append(
            {
                "run_id": f"adapter_run_{index:032x}",
                "scan_id": scan_id,
                "adapter_id": adapter,
                "status": "AVAILABLE",
                "version": "1.0.0",
                "effect_class": (
                    "OBSERVATION_WITH_OPERATIONAL_WRITES"
                    if adapter in {"conda", "docker"}
                    else "PURE_QUERY"
                ),
                "started_at": NOW,
                "finished_at": NOW,
                "completed": True,
                "executable": None,
                "detail": "Synthetic available product fixture.",
                "resources": 0,
                "issues": [],
                "evidence_ids": [],
            }
        )
    return {
        "schema_version": "1.0.0",
        "generated_at": NOW,
        "scan": {
            "scan_id": scan_id,
            "schema_version": "1.0.0",
            "engine_version": "0.1.0",
            "status": "COMPLETED",
            "started_at": NOW,
            "finished_at": NOW,
            "roots": [],
            "summary": {},
        },
        "resources": [],
        "errors": [],
        "adapter_runs": runs,
        "evidence": [],
        "safety_boundary": {"executable": False, "statement": "Inventory only."},
    }


def _write_procmon_csv(
    path: Path, required_processes: tuple[str, ...]
) -> dict[str, object]:
    headers = ("Process Name", "PID", "Operation", "Path", "Result", "Detail")
    rows = [
        {
            "Process Name": process_name,
            "PID": str(4242 + index),
            "Operation": "Process Create",
            "Path": rf"C:\fixture\bin\{process_name}",
            "Result": "SUCCESS",
            "Detail": f"Command line: {process_name} --read-only-fixture",
        }
        for index, process_name in enumerate(required_processes)
    ] + [
        {
            "Process Name": "python.exe",
            "PID": "4242",
            "Operation": "ReadFile",
            "Path": r"C:\fixture\managed\item.bin",
            "Result": "SUCCESS",
            "Detail": "",
        },
        {
            "Process Name": "python.exe",
            "PID": "4242",
            "Operation": "WriteFile",
            "Path": r"C:\fixture\state\reclaimer.db",
            "Result": "SUCCESS",
            "Detail": "",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    return PROCMON.audit_procmon_csv(
        path,
        protected_roots=(
            PROCMON.parse_root_rule(r"managed_cache=C:\fixture\managed"),
            PROCMON.parse_root_rule(r"user_assets=C:\fixture\protected"),
        ),
        allowed_write_roots=(
            PROCMON.parse_root_rule(r"reclaimer_data=C:\fixture\state"),
        ),
        required_processes=required_processes,
    )


def _build_g2(directory: Path, g1_result: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    product_content = b"synthetic-reclaimer-wheel"
    product_hash = _sha(product_content)
    manifest = _base_manifest("G2", product_hash)
    manifest["scope"]["adapters"] = sorted(G2_ADAPTERS)
    manifest["scope"]["available_adapters"] = sorted(G2_ADAPTERS)
    required_processes = tuple(sorted(set(GATE.G2_REQUIRED_PROCESS_BY_ADAPTER.values())))
    manifest["scope"]["required_process_names"] = list(required_processes)
    product_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_product_fixture02",
        kind="PRODUCT_ARTIFACT",
        filename="reclaimer.whl",
        content=product_content,
    )
    prerequisite_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_g1_result_fixture02",
        kind="GATE_RESULT_JSON",
        filename="g1-result.json",
        content=_json_bytes(g1_result),
    )
    manifest["prerequisites"] = [{"gate": "G1", "evidence_ref": prerequisite_id}]
    pml_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_procmon_pml_fixture02",
        kind="PROCMON_PML",
        filename="trace.pml",
        content=b"synthetic-pml",
        sensitive=True,
    )
    screenshot_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_procmon_screen_fixture02",
        kind="PROCMON_FILTER_SCREENSHOT",
        filename="filter.png",
        content=b"synthetic-png",
        sensitive=True,
    )
    csv_path = directory / "trace.csv"
    validation = _write_procmon_csv(csv_path, required_processes)
    manifest["evidence"].append(
        {
            "evidence_id": "gate_evidence_procmon_csv_fixture02",
            "kind": "PROCMON_CSV",
            "relative_path": "trace.csv",
            "sha256": _sha(csv_path.read_bytes()),
            "bytes": csv_path.stat().st_size,
            "contains_sensitive_data": True,
            "description": "Synthetic ProcMon CSV fixture",
        }
    )
    validation_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_procmon_validation_fixture02",
        kind="PROCMON_VALIDATION_JSON",
        filename="procmon-validation.json",
        content=_json_bytes(validation),
    )
    scan_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_scan_report_fixture02",
        kind="SCAN_REPORT_JSON",
        filename="scan-report.json",
        content=_json_bytes(_scan_report()),
    )
    test_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_test_report_fixture02",
        kind="TEST_REPORT_JSON",
        filename="test-report.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "started_at": NOW,
                "finished_at": NOW,
                "command": ["python.exe", "-m", "pytest"],
                "exit_code": 0,
                "checks": [
                    {
                        "check_id": check_id,
                        "status": "PASS",
                        "duration_ms": 1,
                        "detail": "Synthetic negative-version/CLI test.",
                    }
                    for check_id in {
                        "unknown_versions_inventory_only",
                        "preview_inventory_only",
                    }
                ],
            }
        ),
    )
    collector_sha = _sha(b"collector")
    service_ids: dict[str, str] = {}
    for label, kind in (
        ("before", "SERVICE_STATE_BEFORE_JSON"),
        ("after", "SERVICE_STATE_AFTER_JSON"),
    ):
        service_ids[label] = _write_artifact(
            directory,
            manifest,
            evidence_id=f"gate_evidence_service_{label}_fixture02",
            kind=kind,
            filename=f"service-{label}.json",
            content=_json_bytes(
                {
                    "schema_version": "1.0.0",
                    "captured_at": NOW,
                    "label": label,
                    "process_elevated": False,
                    "collector_sha256": collector_sha,
                    "collector_bytes": 100,
                    "services": [{"name": "ollama", "status": "Running"}],
                    "processes": [{"name": "ollama", "process_ids": [1234]}],
                }
            ),
        )
    procmon_refs = [validation_id]
    ref_map = {
        "inventory_smoke": [scan_id],
        "procmon_capture_complete": [pml_id, screenshot_id],
        "procmon_csv_validated": procmon_refs,
        "no_managed_root_writes": procmon_refs,
        "no_user_asset_writes": procmon_refs,
        "allowed_operational_writes_declared": procmon_refs,
        "no_service_start": [service_ids["before"], service_ids["after"]],
        "no_npm_verify": procmon_refs,
        "no_dism": procmon_refs,
        "unknown_versions_inventory_only": [scan_id, test_id],
        "size_semantics_reviewed": [scan_id],
        "preview_inventory_only": [scan_id, test_id],
    }
    for check_id in sorted(G2_CHECKS):
        manifest["checks"].append(
            {
                "check_id": check_id,
                "status": "PASS",
                "required": True,
                "evidence_refs": ref_map[check_id],
                "explanation": "Synthetic test fixture.",
            }
        )
    assert product_id
    manifest_path = directory / "g2-manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest_path


def _prerequisite_result(gate: str, product_hash: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "verdict": "PASS",
        "matrix": gate if gate in {"G1", "G2"} else None,
        "gate": gate if gate in {"G3", "G4"} else None,
        "matrix_errors": [],
        "product_binding": {
            "version": "0.1.0",
            "source_revision": REVISION,
            "artifact_sha256": product_hash,
        },
        "manifests": [
            {
                "manifest_sha256": _sha(gate.encode()),
                "gate": gate,
                "run_id": f"gate_run_{gate.casefold()}_prerequisite",
                "passed": True,
                "hard_errors": [],
                "completion_errors": [],
            }
        ],
    }


def _build_g5(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    product_content = b"synthetic-reclaimer-wheel"
    product_hash = _sha(product_content)
    manifest = _base_manifest("G5", product_hash)
    manifest["environment"]["machine_kind"] = "DISPOSABLE_VM"
    manifest["scope"] = {
        "adapters": ["direct_fs_fixture"],
        "available_adapters": ["direct_fs_fixture"],
        "required_process_names": ["reclaimer-race-fixture.exe"],
        "managed_root_labels": ["approved_fixture_root"],
        "protected_asset_labels": ["user_assets"],
        "allowed_write_root_labels": ["race_control"],
    }
    manifest["review"] = {
        "author_ids": ["author_fixture"],
        "reviewer_ids": ["reviewer_fixture"],
        "review_method": "HUMAN",
        "reviewed_at": NOW,
        "notes": "Synthetic independent-review fixture.",
    }
    _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_product_fixture05",
        kind="PRODUCT_ARTIFACT",
        filename="reclaimer.whl",
        content=product_content,
    )
    prerequisites = []
    for gate in ("G2", "G3", "G4"):
        evidence_id = f"gate_evidence_{gate.casefold()}_result_fixture05"
        _write_artifact(
            directory,
            manifest,
            evidence_id=evidence_id,
            kind="GATE_RESULT_JSON",
            filename=f"{gate.casefold()}-result.json",
            content=_json_bytes(_prerequisite_result(gate, product_hash)),
        )
        prerequisites.append({"gate": gate, "evidence_ref": evidence_id})
    manifest["prerequisites"] = prerequisites
    races = []
    for check_id in sorted(G5_RACES):
        digest = _sha(check_id.encode())
        races.append(
            {
                "check_id": check_id,
                "attempts": 10_000,
                "barrier_hits": 10_000,
                "safe_skips": 10_000,
                "identity_mismatch_skips": 10_000,
                "lock_skips": 10_000 if check_id == "locked_file_race" else 0,
                "unexpected_mutations": 0,
                "action_scope_expansions": 0,
                "seed_commitment_sha256": _sha(f"seed-{check_id}".encode()),
                "protected_canary_before_sha256": digest,
                "protected_canary_after_sha256": digest,
                "duration_ms": 1,
                "result": "PASS",
            }
        )
    race_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_race_report_fixture05",
        kind="RACE_REPORT_JSON",
        filename="race-report.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "captured_at": NOW,
                "filesystem": "NTFS",
                "volume_kind": "FIXED",
                "races": races,
                "unexpected_mutations": 0,
                "action_scope_expansions": 0,
                "result": "PASS",
            }
        ),
    )
    canary_digest = _sha(b"protected-canary")
    canary_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_canary_report_fixture05",
        kind="CANARY_ATTESTATION_JSON",
        filename="canary-report.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "captured_at": NOW,
                "groups": [
                    {
                        "label": "user_assets",
                        "object_count": 10,
                        "before_digest": canary_digest,
                        "after_digest": canary_digest,
                        "file_identities_verified": True,
                        "unexpected_mutations": 0,
                    }
                ],
                "unexpected_mutations": 0,
                "result": "PASS",
            }
        ),
    )
    static_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_static_audit_fixture05",
        kind="STATIC_AUDIT_REPORT",
        filename="static-audit.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "reviewed_at": NOW,
                "mutation_apis": ["SetFileInformationByHandle(FileDispositionInfo)"],
                "prohibited_api_hits": {
                    "shutil.rmtree": 0,
                    "os.remove": 0,
                    "os.unlink": 0,
                    "os.rmdir": 0,
                    "DeleteFileW": 0,
                    "shell_delete": 0,
                    "string_path_fallback": 0,
                },
                "approved_root_handle_pinned": True,
                "fail_closed_branches_reviewed": True,
                "no_fallback_delete": True,
                "deny_list_version": "deny-v1",
                "deny_list_patterns": [
                    ".git",
                    "lockfile",
                    ".env*",
                    "*.key",
                    "*.pem",
                    "globalStorage",
                    "Local History",
                    ".codex",
                    ".claude",
                ],
                "conclusion": "PASS",
            }
        ),
    )
    review_id = _write_artifact(
        directory,
        manifest,
        evidence_id="gate_evidence_review_fixture05",
        kind="REVIEW_ATTESTATION",
        filename="review.json",
        content=_json_bytes(
            {
                "schema_version": "1.0.0",
                "artifact_sha256": product_hash,
                "source_revision": REVISION,
                "author_ids": ["author_fixture"],
                "reviewer_ids": ["reviewer_fixture"],
                "review_method": "HUMAN",
                "reviewed_at": NOW,
                "open_findings": [],
                "conclusion": "PASS",
            }
        ),
    )
    refs = {
        **{check_id: [race_id] for check_id in G5_RACES},
        "protected_canaries_unchanged": [canary_id],
        "identity_mismatch_skips": [race_id],
        "approved_root_handle_pinned": [race_id],
        "deny_list_defense_in_depth": [static_id],
        "no_fallback_delete": [static_id],
        "local_fixed_ntfs_only": [race_id],
        "independent_review": [review_id],
    }
    for check_id in sorted(G5_CHECKS):
        check = {
            "check_id": check_id,
            "status": "PASS",
            "required": True,
            "evidence_refs": refs[check_id],
            "explanation": "Synthetic G5 contract fixture.",
        }
        if check_id in G5_RACES:
            check["iterations"] = 10_000
        manifest["checks"].append(check)
    manifest_path = directory / "g5-manifest.json"
    manifest_path.write_bytes(_json_bytes(manifest))
    return manifest_path


def test_complete_synthetic_g1_manifest_passes_closed_contract(tmp_path: Path) -> None:
    manifest_path, _ = _build_g1(tmp_path / "g1")

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert result.passed
    assert result.hard_errors == []
    assert result.completion_errors == []


def test_gate_manifest_rejects_non_git_source_revision(tmp_path: Path) -> None:
    manifest_path, _ = _build_g1(tmp_path / "g1")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["product"]["source_revision"] = "release-candidate-1"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert (
        "source revision must be a full 40- or 64-character lowercase Git object ID"
        in result.completion_errors
    )


def test_complete_synthetic_g0_contract_requires_owner_and_real_ci_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _build_g0(tmp_path / "g0")

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert result.passed, (result.hard_errors, result.completion_errors)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    owner_entry = next(
        entry for entry in manifest["evidence"] if entry["kind"] == "OWNER_ATTESTATION_JSON"
    )
    owner_path = manifest_path.parent / owner_entry["relative_path"]
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["approved"] = False
    content = _json_bytes(owner)
    owner_path.write_bytes(content)
    owner_entry["sha256"] = _sha(content)
    owner_entry["bytes"] = len(content)
    manifest_path.write_bytes(_json_bytes(manifest))
    rejected = validate_manifest(manifest_path, SCHEMA_VALIDATOR)
    assert not rejected.passed
    assert any("owner" in error.casefold() for error in rejected.completion_errors)


def test_g0_rejects_noncanonical_license_digest(tmp_path: Path) -> None:
    manifest_path = _build_g0(tmp_path / "g0")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_entry = next(
        entry for entry in manifest["evidence"] if entry["kind"] == "SOURCE_AUDIT_JSON"
    )
    source_path = manifest_path.parent / source_entry["relative_path"]
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["license_sha256"] = _sha(b"short synchronized license")
    content = _json_bytes(source)
    source_path.write_bytes(content)
    source_entry["sha256"] = _sha(content)
    source_entry["bytes"] = len(content)
    manifest_path.write_bytes(_json_bytes(manifest))

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert any("mechanical G0 boundary" in error for error in result.completion_errors)


def test_g1_manifest_fails_if_boundary_canary_digest_changes(tmp_path: Path) -> None:
    manifest_path, manifest = _build_g1(tmp_path / "g1")
    boundary_entry = next(
        entry for entry in manifest["evidence"] if entry["kind"] == "BOUNDARY_OBSERVATION_JSON"
    )
    boundary_path = manifest_path.parent / boundary_entry["relative_path"]
    boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
    boundary["observations"][0]["after_identity_digest"] = _sha(b"changed")
    content = _json_bytes(boundary)
    boundary_path.write_bytes(content)
    boundary_entry["sha256"] = _sha(content)
    boundary_entry["bytes"] = len(content)
    manifest_path.write_bytes(_json_bytes(manifest))

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert any("boundary observation" in error for error in result.hard_errors)


def test_complete_synthetic_g2_manifest_passes_with_g1_binding(tmp_path: Path) -> None:
    g1_manifest_path, _ = _build_g1(tmp_path / "g1")
    g1_manifest_result = validate_manifest(g1_manifest_path, SCHEMA_VALIDATOR)
    g1_result = GATE._result_payload([g1_manifest_result], "G1", [])
    g2_manifest_path = _build_g2(tmp_path / "g2", g1_result)

    result = validate_manifest(g2_manifest_path, SCHEMA_VALIDATOR)

    assert result.passed, (result.hard_errors, result.completion_errors)


def test_g2_available_cli_adapter_requires_its_procmon_process(tmp_path: Path) -> None:
    g1_manifest_path, _ = _build_g1(tmp_path / "g1")
    g1_manifest_result = validate_manifest(g1_manifest_path, SCHEMA_VALIDATOR)
    g1_result = GATE._result_payload([g1_manifest_result], "G1", [])
    g2_manifest_path = _build_g2(tmp_path / "g2", g1_result)
    manifest = json.loads(g2_manifest_path.read_text(encoding="utf-8"))
    manifest["scope"]["required_process_names"].remove("docker.exe")
    g2_manifest_path.write_bytes(_json_bytes(manifest))

    result = validate_manifest(g2_manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert any("docker.exe" in error for error in result.completion_errors)


def test_g2_fails_when_available_adapter_declaration_disagrees_with_report(
    tmp_path: Path,
) -> None:
    g1_manifest_path, _ = _build_g1(tmp_path / "g1")
    g1_manifest_result = validate_manifest(g1_manifest_path, SCHEMA_VALIDATOR)
    g1_result = GATE._result_payload([g1_manifest_result], "G1", [])
    g2_manifest_path = _build_g2(tmp_path / "g2", g1_result)
    manifest = json.loads(g2_manifest_path.read_text(encoding="utf-8"))
    scan_entry = next(
        entry for entry in manifest["evidence"] if entry["kind"] == "SCAN_REPORT_JSON"
    )
    scan_path = g2_manifest_path.parent / scan_entry["relative_path"]
    report = json.loads(scan_path.read_text(encoding="utf-8"))
    report["adapter_runs"][0]["status"] = "UNAVAILABLE"
    content = _json_bytes(report)
    scan_path.write_bytes(content)
    scan_entry["sha256"] = _sha(content)
    scan_entry["bytes"] = len(content)
    g2_manifest_path.write_bytes(_json_bytes(manifest))

    result = validate_manifest(g2_manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert any("AVAILABLE" in error for error in result.hard_errors)


def test_gate_validator_rejects_hardlinked_evidence(tmp_path: Path) -> None:
    manifest_path, manifest = _build_g1(tmp_path / "g1")
    product_entry = next(
        entry for entry in manifest["evidence"] if entry["kind"] == "PRODUCT_ARTIFACT"
    )
    product = manifest_path.parent / product_entry["relative_path"]
    os.link(product, manifest_path.parent / "product-alias.whl")

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert not result.passed
    assert any("regular file" in error for error in result.hard_errors)


def test_complete_synthetic_g5_contract_passes_only_with_prerequisites(tmp_path: Path) -> None:
    manifest_path = _build_g5(tmp_path / "g5")

    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)

    assert result.passed, (result.hard_errors, result.completion_errors)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prerequisites"] = manifest["prerequisites"][:-1]
    manifest_path.write_bytes(_json_bytes(manifest))
    missing = validate_manifest(manifest_path, SCHEMA_VALIDATOR)
    assert not missing.passed
    assert any("G4" in error for error in missing.completion_errors)


def test_g2_matrix_requires_two_physical_machines_and_one_vm(tmp_path: Path) -> None:
    g1_manifest_path, _ = _build_g1(tmp_path / "g1")
    g1_result_obj = validate_manifest(g1_manifest_path, SCHEMA_VALIDATOR)
    g1_result = GATE._result_payload([g1_result_obj], "G1", [])
    base_path = _build_g2(tmp_path / "g2", g1_result)
    base = validate_manifest(base_path, SCHEMA_VALIDATOR)
    assert base.passed
    results = []
    for index, kind in enumerate(("PHYSICAL", "PHYSICAL", "DISPOSABLE_VM")):
        payload = deepcopy(base.payload)
        assert payload is not None
        payload["run_id"] = f"gate_run_g2_matrix{index:02d}"
        payload["environment"]["machine_kind"] = kind
        payload["environment"]["locale"] = "zh-CN" if index == 0 else "en-US"
        payload["environment"]["machine_fingerprint_sha256"] = _sha(
            f"machine-{index}".encode()
        )
        results.append(GATE.ManifestResult("f" * 64, payload, [], []))

    assert validate_matrix("G2", results) == []
    for result in results:
        result.payload["scope"]["available_adapters"].remove("ollama")
    assert "ollama" in " ".join(validate_matrix("G2", results))
    for result in results:
        result.payload["scope"]["available_adapters"].append("ollama")
    results[2].payload["environment"]["machine_kind"] = "PHYSICAL"
    assert "disposable VM" in " ".join(validate_matrix("G2", results))


def test_gate_environment_and_g2_matrix_enforce_supported_platform_locale(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _build_g1(tmp_path / "g1")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["environment"]["architecture"] = "arm64"
    manifest_path.write_bytes(_json_bytes(manifest))
    result = validate_manifest(manifest_path, SCHEMA_VALIDATOR)
    assert not result.passed
    assert any("x86_64" in error for error in result.completion_errors)

    payloads = []
    for index, kind in enumerate(("PHYSICAL", "PHYSICAL", "DISPOSABLE_VM")):
        payload = deepcopy(result.payload)
        assert payload is not None
        payload["environment"]["architecture"] = "x86_64"
        payload["environment"]["machine_kind"] = kind
        payload["environment"]["machine_fingerprint_sha256"] = _sha(
            f"locale-machine-{index}".encode()
        )
        payload["run_id"] = f"gate_run_g2_locale{index:02d}"
        payload["gate"] = "G2"
        payloads.append(GATE.ManifestResult("e" * 64, payload, [], []))
    assert "zh-CN" in " ".join(validate_matrix("G2", payloads))
