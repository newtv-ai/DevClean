"""Validate G0/G1/G2/G5 external evidence manifests and machine matrices.

The validator reads and hashes evidence artifacts but never invokes DevClean,
vendor tools, services, or cleanup actions.  A valid individual G2 run is not a
G2 gate pass: use ``--matrix G2`` with two physical-machine manifests and one
disposable-VM manifest bound to the same product artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing import Resource as SchemaResource

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "schemas" / "gate-evidence.schema.json"
MAX_JSON_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
MAX_REPORTED_ERRORS = 32
READ_ONLY_EFFECT_CLASSES = {"PURE_QUERY", "OBSERVATION_WITH_OPERATIONAL_WRITES"}
G2_ADAPTERS = {
    "huggingface",
    "pip",
    "uv",
    "conda",
    "npm",
    "pnpm",
    "docker",
    "ollama",
    "vscode",
}
G2_REQUIRED_PROCESS_BY_ADAPTER = {
    "huggingface": "hf.exe",
    "pip": "python.exe",
    "uv": "uv.exe",
    "conda": "conda.exe",
    "npm": "node.exe",
    "docker": "docker.exe",
}
REPARSE_POINT_ATTRIBUTE = 0x00000400
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_REVISION_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
CANONICAL_GPLV3_SHA256 = (
    "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"
)

G0_REQUIRED_CHECKS = {
    "license_owner_confirmed",
    "third_party_boundary_clean",
    "windows_ci_matrix_passed",
    "codeql_passed",
    "dependency_audit_clean",
    "release_artifacts_validated",
    "preview_inventory_only",
}

G1_REQUIRED_CHECKS = {
    "symlink_not_descended",
    "junction_not_descended",
    "mount_point_not_descended",
    "junction_loop_bounded",
    "onedrive_placeholder_not_hydrated",
    "unc_no_execution_candidate",
    "network_volume_no_execution_candidate",
    "removable_volume_no_execution_candidate",
    "refs_volume_no_execution_candidate",
    "million_resource_streaming",
    "cancellation_under_two_seconds",
    "access_denied_no_uac",
    "inventory_only_no_delete",
}
G1_BOUNDARY_CHECKS = {
    "symlink_not_descended",
    "junction_not_descended",
    "mount_point_not_descended",
    "junction_loop_bounded",
    "onedrive_placeholder_not_hydrated",
    "unc_no_execution_candidate",
    "network_volume_no_execution_candidate",
    "removable_volume_no_execution_candidate",
    "access_denied_no_uac",
}
G1_TEST_CHECKS = G1_REQUIRED_CHECKS - {"million_resource_streaming"}
G2_REQUIRED_CHECKS = {
    "inventory_smoke",
    "procmon_capture_complete",
    "procmon_csv_validated",
    "no_managed_root_writes",
    "no_user_asset_writes",
    "allowed_operational_writes_declared",
    "no_service_start",
    "no_npm_verify",
    "no_dism",
    "unknown_versions_inventory_only",
    "size_semantics_reviewed",
    "preview_inventory_only",
}
G5_RACE_CHECKS = {
    "junction_swap_race",
    "rename_race",
    "file_id_replacement_race",
    "locked_file_race",
    "read_only_file_race",
    "hardlink_race",
    "new_file_insertion_race",
}
G5_REQUIRED_CHECKS = G5_RACE_CHECKS | {
    "protected_canaries_unchanged",
    "identity_mismatch_skips",
    "approved_root_handle_pinned",
    "deny_list_defense_in_depth",
    "no_fallback_delete",
    "local_fixed_ntfs_only",
    "independent_review",
}
REQUIRED_CHECKS = {
    "G0": G0_REQUIRED_CHECKS,
    "G1": G1_REQUIRED_CHECKS,
    "G2": G2_REQUIRED_CHECKS,
    "G5": G5_REQUIRED_CHECKS,
}

G1_REQUIRED_EVIDENCE_KINDS = {
    "PRODUCT_ARTIFACT",
    "TEST_REPORT_JSON",
    "BENCHMARK_JSON",
    "BOUNDARY_OBSERVATION_JSON",
}
G0_REQUIRED_EVIDENCE_KINDS = {
    "PRODUCT_ARTIFACT",
    "OWNER_ATTESTATION_JSON",
    "SOURCE_AUDIT_JSON",
    "CI_ATTESTATION_JSON",
    "CODEQL_ATTESTATION_JSON",
    "DEPENDENCY_AUDIT_JSON",
    "RELEASE_VALIDATION_JSON",
}
G2_REQUIRED_EVIDENCE_KINDS = {
    "GATE_RESULT_JSON",
    "PRODUCT_ARTIFACT",
    "PROCMON_PML",
    "PROCMON_CSV",
    "PROCMON_FILTER_SCREENSHOT",
    "PROCMON_VALIDATION_JSON",
    "SCAN_REPORT_JSON",
    "TEST_REPORT_JSON",
    "SERVICE_STATE_BEFORE_JSON",
    "SERVICE_STATE_AFTER_JSON",
}
G5_REQUIRED_EVIDENCE_KINDS = {
    "GATE_RESULT_JSON",
    "PRODUCT_ARTIFACT",
    "RACE_REPORT_JSON",
    "CANARY_ATTESTATION_JSON",
    "STATIC_AUDIT_REPORT",
    "REVIEW_ATTESTATION",
}
REQUIRED_EVIDENCE_KINDS = {
    "G0": G0_REQUIRED_EVIDENCE_KINDS,
    "G1": G1_REQUIRED_EVIDENCE_KINDS,
    "G2": G2_REQUIRED_EVIDENCE_KINDS,
    "G5": G5_REQUIRED_EVIDENCE_KINDS,
}


@dataclass(frozen=True)
class Artifact:
    evidence_id: str
    kind: str
    path: Path
    sha256: str
    bytes: int
    contains_sensitive_data: bool


@dataclass
class ManifestResult:
    manifest_sha256: str
    payload: dict[str, Any] | None
    hard_errors: list[str]
    completion_errors: list[str]

    @property
    def gate(self) -> str | None:
        value = self.payload.get("gate") if self.payload else None
        return value if isinstance(value, str) else None

    @property
    def run_id(self) -> str | None:
        value = self.payload.get("run_id") if self.payload else None
        return value if isinstance(value, str) else None

    @property
    def passed(self) -> bool:
        return (
            not self.hard_errors
            and not self.completion_errors
            and self.payload is not None
            and self.payload.get("conclusion") == "PASS"
        )


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    return bool(
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and (not before.st_dev or not after.st_dev or before.st_dev == after.st_dev)
        and (not before.st_ino or not after.st_ino or before.st_ino == after.st_ino)
    )


def _hash_file_stable(path: Path, *, max_bytes: int) -> tuple[str, int]:
    before = path.stat(follow_symlinks=False)
    if before.st_size < 1 or before.st_size > max_bytes:
        raise ValueError(f"evidence size is outside its bound: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if not _same_file_state(before, after):
        raise RuntimeError(f"evidence changed while hashing: {path.name}")
    return digest.hexdigest(), before.st_size


def _sha256_file(path: Path) -> str:
    return _hash_file_stable(path, max_bytes=MAX_EVIDENCE_ARTIFACT_BYTES)[0]


def _read_stable_bytes(path: Path, *, max_bytes: int) -> bytes:
    before = path.stat(follow_symlinks=False)
    if before.st_size < 1 or before.st_size > max_bytes:
        raise ValueError(f"JSON evidence size is outside its bound: {path.name}")
    content = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if len(content) != before.st_size or not _same_file_state(before, after):
        raise RuntimeError(f"JSON evidence changed while reading: {path.name}")
    return content


def _schema_validator(schema_path: Path) -> Draft202012Validator:
    schema_payload = strict_json_loads(
        _read_stable_bytes(schema_path, max_bytes=MAX_MANIFEST_BYTES).decode(
            "utf-8", errors="strict"
        )
    )
    if not isinstance(schema_payload, dict):
        raise ValueError("gate schema root must be an object")
    Draft202012Validator.check_schema(schema_payload)
    return Draft202012Validator(schema_payload, format_checker=FormatChecker())


@lru_cache(maxsize=8)
def _local_schema_validator(filename: str) -> Draft202012Validator:
    resources: list[tuple[str, SchemaResource[Any]]] = []
    target: dict[str, Any] | None = None
    for path in sorted((ROOT / "schemas").glob("*.schema.json")):
        payload = strict_json_loads(
            _read_stable_bytes(path, max_bytes=MAX_MANIFEST_BYTES).decode(
                "utf-8", errors="strict"
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("$id"), str):
            raise ValueError(f"local schema has no object/$id contract: {path.name}")
        Draft202012Validator.check_schema(payload)
        resources.append((payload["$id"], SchemaResource.from_contents(payload)))
        if path.name == filename:
            target = payload
    if target is None:
        raise ValueError(f"local schema does not exist: {filename}")
    registry = Registry().with_resources(resources)
    return Draft202012Validator(
        target,
        registry=registry,
        format_checker=FormatChecker(),
    )


def _bounded_schema_errors(
    validator: Draft202012Validator, payload: Any
) -> Iterable[str]:
    for index, error in enumerate(validator.iter_errors(payload)):
        if index >= MAX_REPORTED_ERRORS:
            yield "schema: additional errors were truncated"
            return
        yield _schema_error_message(error)


def _schema_error_message(error: Any) -> str:
    location = "/".join(str(part) for part in error.absolute_path)
    return f"schema {location or '<root>'}: {error.message}"


def _artifact_path(manifest_path: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if (
        pure.is_absolute()
        or pure.as_posix() != relative_path
        or "\\" in relative_path
        or ":" in relative_path
        or "\x00" in relative_path
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise ValueError(f"unsafe evidence relative_path: {relative_path!r}")
    base = Path(os.path.abspath(manifest_path.parent))
    candidate = base.joinpath(*pure.parts)
    try:
        common = os.path.commonpath((str(base), str(candidate)))
    except ValueError as error:
        raise ValueError(f"evidence escapes manifest volume: {relative_path!r}") from error
    if os.path.normcase(common) != os.path.normcase(str(base)):
        raise ValueError(f"evidence escapes manifest directory: {relative_path!r}")
    _reject_reparse_chain(base)
    current = base
    for part in pure.parts:
        current /= part
        _reject_reparse_path(current)
    state = candidate.stat(follow_symlinks=False)
    if not candidate.is_file() or state.st_nlink != 1:
        raise ValueError(f"evidence is not a regular file: {relative_path!r}")
    return candidate


def _reject_reparse_chain(path: Path) -> None:
    current = path
    while True:
        _reject_reparse_path(current)
        if current == current.parent:
            return
        current = current.parent


def _reject_reparse_path(path: Path) -> None:
    state = path.stat(follow_symlinks=False)
    if path.is_symlink() or int(
        getattr(state, "st_file_attributes", 0)
    ) & REPARSE_POINT_ATTRIBUTE:
        raise ValueError(f"evidence path crosses a symlink/reparse point: {path.name}")


def _verify_artifacts(
    manifest_path: Path,
    payload: dict[str, Any],
    hard_errors: list[str],
) -> dict[str, Artifact]:
    artifacts: dict[str, Artifact] = {}
    paths: set[str] = set()
    for entry in payload["evidence"]:
        evidence_id = entry["evidence_id"]
        if evidence_id in artifacts:
            hard_errors.append(f"duplicate evidence_id: {evidence_id}")
            continue
        try:
            path = _artifact_path(manifest_path, entry["relative_path"])
        except (OSError, ValueError) as exc:
            hard_errors.append(str(exc))
            continue
        path_key = os.path.normcase(str(path))
        if path_key in paths:
            hard_errors.append("two evidence records resolve to the same file")
            continue
        paths.add(path_key)
        try:
            actual_sha256, actual_size = _hash_file_stable(
                path,
                max_bytes=MAX_EVIDENCE_ARTIFACT_BYTES,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            hard_errors.append(f"{evidence_id}: unable to hash stable evidence: {exc}")
            continue
        if actual_size != entry["bytes"]:
            hard_errors.append(f"{evidence_id}: byte length does not match the file")
        if actual_sha256 != entry["sha256"]:
            hard_errors.append(f"{evidence_id}: SHA-256 does not match the file")
        artifacts[evidence_id] = Artifact(
            evidence_id=evidence_id,
            kind=entry["kind"],
            path=path,
            sha256=actual_sha256,
            bytes=actual_size,
            contains_sensitive_data=entry["contains_sensitive_data"],
        )
    return artifacts


def _check_references(
    payload: dict[str, Any], artifacts: dict[str, Artifact], hard_errors: list[str]
) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for check in payload["checks"]:
        check_id = check["check_id"]
        if check_id in checks:
            hard_errors.append(f"duplicate check_id: {check_id}")
            continue
        checks[check_id] = check
        missing = sorted(set(check["evidence_refs"]) - artifacts.keys())
        if missing:
            hard_errors.append(f"{check_id}: unknown evidence refs: {', '.join(missing)}")
    return checks


def _require_check_kinds(
    check_id: str,
    required_kinds: set[str],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
) -> None:
    check = checks.get(check_id)
    if check is None:
        return
    actual = {
        artifacts[evidence_id].kind
        for evidence_id in check["evidence_refs"]
        if evidence_id in artifacts
    }
    missing = sorted(required_kinds - actual)
    if missing:
        hard_errors.append(f"{check_id}: evidence refs lack kinds: {', '.join(missing)}")


def _read_json_artifact(artifact: Artifact, hard_errors: list[str]) -> dict[str, Any] | None:
    try:
        content = _read_stable_bytes(artifact.path, max_bytes=MAX_JSON_ARTIFACT_BYTES)
        if hashlib.sha256(content).hexdigest() != artifact.sha256:
            raise RuntimeError("JSON evidence changed after manifest hashing")
        parsed = strict_json_loads(content.decode("utf-8", errors="strict"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON evidence root must be an object")
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        hard_errors.append(f"{artifact.evidence_id}: invalid JSON evidence: {exc}")
        return None
    return parsed


def _one_artifact(
    artifacts: dict[str, Artifact], kind: str, hard_errors: list[str]
) -> Artifact | None:
    matches = [artifact for artifact in artifacts.values() if artifact.kind == kind]
    if len(matches) > 1:
        hard_errors.append(f"multiple {kind} artifacts are ambiguous")
    return matches[0] if len(matches) == 1 else None


def _validate_product_binding(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    completion_errors: list[str],
) -> None:
    matches = [artifact for artifact in artifacts.values() if artifact.kind == "PRODUCT_ARTIFACT"]
    if len(matches) != 1:
        completion_errors.append("exactly one PRODUCT_ARTIFACT is required")
        return
    if matches[0].sha256 != payload["product"]["artifact_sha256"]:
        completion_errors.append("product.artifact_sha256 is not bound to PRODUCT_ARTIFACT")


def _validate_procmon_binding(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    validation_artifact = _one_artifact(artifacts, "PROCMON_VALIDATION_JSON", hard_errors)
    csv_artifact = _one_artifact(artifacts, "PROCMON_CSV", hard_errors)
    if validation_artifact is None or csv_artifact is None:
        return
    validation = _read_json_artifact(validation_artifact, hard_errors)
    if validation is None:
        return
    if validation.get("schema_version") != "1.0.0":
        hard_errors.append("ProcMon validation schema_version is not 1.0.0")
    if validation.get("csv_sha256") != csv_artifact.sha256:
        hard_errors.append("ProcMon validation is not bound to the PROCMON_CSV SHA-256")
    if validation.get("verdict") != "PASS":
        completion_errors.append("ProcMon validation verdict is not PASS")
    if validation.get("violation_count") != 0:
        completion_errors.append("ProcMon validation contains violations")
    if validation.get("violations") != [] or validation.get("violations_truncated") is not False:
        completion_errors.append(
            "ProcMon validation does not contain a complete empty violation set"
        )
    if validation.get("csv_bytes") != csv_artifact.bytes:
        hard_errors.append("ProcMon validation byte count is not bound to PROCMON_CSV")
    if validation.get("non_loopback_network_count") != 0:
        completion_errors.append("ProcMon validation observed non-loopback network activity")

    scope = payload["scope"]
    if not set(scope["available_adapters"]) <= set(scope["adapters"]):
        completion_errors.append("available adapters are not a subset of adapter scope")
    expected_processes = sorted(
        process.casefold() for process in scope["required_process_names"]
    )
    if validation.get("required_processes") != expected_processes:
        hard_errors.append("ProcMon required processes do not match the manifest scope")
    if validation.get("observed_required_processes") != expected_processes:
        completion_errors.append("ProcMon did not observe every manifest-required process")
    expected_protected = sorted(
        set(scope["managed_root_labels"]) | set(scope["protected_asset_labels"])
    )
    if validation.get("protected_root_labels") != expected_protected:
        hard_errors.append("ProcMon protected-root labels do not match the manifest scope")
    if validation.get("allowed_write_root_labels") != sorted(
        scope["allowed_write_root_labels"]
    ):
        hard_errors.append("ProcMon allowed-write labels do not match the manifest scope")

    rows = validation.get("rows")
    operation_counts = validation.get("operation_counts")
    classification_counts = validation.get("classification_counts")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1:
        hard_errors.append("ProcMon validation row count is invalid")
    for label, counts in (
        ("operation", operation_counts),
        ("classification", classification_counts),
    ):
        if (
            not isinstance(counts, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for key, value in counts.items()
            )
            or isinstance(rows, bool)
            or not isinstance(rows, int)
            or sum(counts.values()) != rows
        ):
            hard_errors.append(f"ProcMon {label} counts do not sum to rows")

    network_count = validation.get("network_event_count")
    loopback_count = validation.get("loopback_network_event_count")
    non_loopback_count = validation.get("non_loopback_network_count")
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (network_count, loopback_count, non_loopback_count)
        )
        or loopback_count + non_loopback_count != network_count
    ):
        hard_errors.append("ProcMon network counters are inconsistent")
    max_rows = validation.get("max_rows")
    if (
        not isinstance(max_rows, int)
        or isinstance(max_rows, bool)
        or not isinstance(rows, int)
        or isinstance(rows, bool)
        or max_rows < rows
    ):
        hard_errors.append("ProcMon max_rows boundary is invalid")
    if validation.get("violation_code_counts") != {}:
        hard_errors.append("ProcMon PASS contains non-empty violation-code counts")

    allowed_count = validation.get("allowed_write_count")
    allowed_summary = validation.get("allowed_write_summary")
    if not isinstance(allowed_count, int) or isinstance(allowed_count, bool) or allowed_count < 0:
        hard_errors.append("ProcMon allowed-write count is invalid")
    elif not isinstance(allowed_summary, list):
        hard_errors.append("ProcMon allowed-write summary is missing")
    else:
        summary_count = 0
        for item in allowed_summary:
            if (
                not isinstance(item, dict)
                or item.get("matched_root") not in scope["allowed_write_root_labels"]
                or not isinstance(item.get("operation"), str)
                or not isinstance(item.get("result"), str)
                or not isinstance(item.get("count"), int)
                or isinstance(item.get("count"), bool)
                or item["count"] < 1
            ):
                hard_errors.append("ProcMon allowed-write summary entry is invalid")
                break
            summary_count += item["count"]
        if summary_count != allowed_count:
            hard_errors.append("ProcMon allowed-write summary does not match its count")
    validator_sha256 = validation.get("validator_sha256")
    if not isinstance(validator_sha256, str) or not SHA256_RE.fullmatch(validator_sha256):
        hard_errors.append("ProcMon validator SHA-256 is missing or invalid")

    for kind in {"PROCMON_PML", "PROCMON_CSV", "PROCMON_FILTER_SCREENSHOT"}:
        artifact = _one_artifact(artifacts, kind, hard_errors)
        if artifact is not None and not artifact.contains_sensitive_data:
            hard_errors.append(f"{kind} must be marked contains_sensitive_data=true")


def _validate_scan_report(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "SCAN_REPORT_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    schema_errors = list(
        _bounded_schema_errors(
            _local_schema_validator("scan-report.schema.json"),
            report,
        )
    )
    if schema_errors:
        hard_errors.extend(f"scan report {error}" for error in schema_errors)
        return
    boundary = report.get("safety_boundary")
    if not isinstance(boundary, dict) or boundary.get("executable") is not False:
        completion_errors.append("scan report safety_boundary.executable is not false")
    resources = report.get("resources")
    if not isinstance(resources, list):
        hard_errors.append("scan report resources must be an array")
    elif any(
        not isinstance(item, dict) or item.get("actionable") is not False
        for item in resources
    ):
        completion_errors.append("scan report contains a missing/true actionable flag")
    adapter_runs = report.get("adapter_runs")
    if not isinstance(adapter_runs, list):
        hard_errors.append("scan report adapter_runs must be an array")
    else:
        observed_effects = {
            item.get("effect_class")
            for item in adapter_runs
            if isinstance(item, dict)
        }
        if not observed_effects <= READ_ONLY_EFFECT_CLASSES:
            completion_errors.append("scan report includes a non-inventory effect class")
        expected_adapters = set(payload["scope"]["adapters"])
        observed_adapters = {
            item.get("adapter_id") for item in adapter_runs if isinstance(item, dict)
        }
        if observed_adapters != expected_adapters:
            completion_errors.append(
                "scan report adapter set differs from manifest scope"
            )
        if any(
            isinstance(item, dict) and item.get("status") == "ERROR"
            for item in adapter_runs
        ):
            completion_errors.append("scan report contains an adapter ERROR result")
        available_adapters = {
            item.get("adapter_id")
            for item in adapter_runs
            if isinstance(item, dict) and item.get("status") == "AVAILABLE"
        }
        declared_available = set(payload["scope"]["available_adapters"])
        if available_adapters != declared_available:
            hard_errors.append(
                "scan report AVAILABLE adapters do not match manifest scope declaration"
            )

    scan = report.get("scan")
    if not isinstance(scan, dict):
        hard_errors.append("scan report scan object is missing")
    else:
        if scan.get("status") != "COMPLETED":
            completion_errors.append("scan report did not complete")
        if scan.get("engine_version") != payload["product"]["version"]:
            hard_errors.append("scan report engine version does not match product version")

    evidence_records = report.get("evidence")
    if not isinstance(evidence_records, list):
        hard_errors.append("scan report evidence must be an array")
        return
    evidence_ids = {
        item.get("evidence_id") for item in evidence_records if isinstance(item, dict)
    }
    if None in evidence_ids or len(evidence_ids) != len(evidence_records):
        hard_errors.append("scan report evidence IDs are missing or duplicated")
        return
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            for item in resource.get("evidence", []):
                if not isinstance(item, dict):
                    continue
                source = item.get("source")
                if isinstance(source, str) and source.startswith("evidence:") and (
                    source.removeprefix("evidence:") not in evidence_ids
                ):
                    hard_errors.append("scan report resource has a dangling evidence reference")
                    return
    if isinstance(adapter_runs, list):
        for run in adapter_runs:
            if not isinstance(run, dict):
                continue
            references = run.get("evidence_ids", [])
            if isinstance(references, list) and any(
                evidence_id not in evidence_ids for evidence_id in references
            ):
                hard_errors.append("scan report adapter run has a dangling evidence reference")
                return


def _service_projection(
    artifact: Artifact,
    expected_label: str,
    hard_errors: list[str],
    completion_errors: list[str],
) -> dict[str, Any] | None:
    payload = _read_json_artifact(artifact, hard_errors)
    if payload is None:
        return None
    expected_keys = {
        "schema_version",
        "captured_at",
        "label",
        "process_elevated",
        "collector_sha256",
        "collector_bytes",
        "services",
        "processes",
    }
    if set(payload) != expected_keys or payload.get("schema_version") != "1.0.0":
        hard_errors.append(f"{artifact.evidence_id}: service snapshot contract is invalid")
        return None
    if payload.get("label") != expected_label:
        hard_errors.append(f"{artifact.evidence_id}: service snapshot label is invalid")
    if payload.get("process_elevated") is not False:
        completion_errors.append(f"{artifact.evidence_id}: service collector was elevated")
    collector_sha256 = payload.get("collector_sha256")
    if not isinstance(collector_sha256, str) or not SHA256_RE.fullmatch(collector_sha256):
        hard_errors.append(f"{artifact.evidence_id}: collector SHA-256 is invalid")
    if (
        not isinstance(payload.get("collector_bytes"), int)
        or isinstance(payload.get("collector_bytes"), bool)
        or payload["collector_bytes"] < 1
    ):
        hard_errors.append(f"{artifact.evidence_id}: collector byte count is invalid")
    try:
        _parse_timestamp(payload.get("captured_at"), "service snapshot captured_at")
    except ValueError as exc:
        hard_errors.append(f"{artifact.evidence_id}: {exc}")
    services = payload.get("services")
    processes = payload.get("processes")
    if not isinstance(services, list) or not isinstance(processes, list):
        hard_errors.append(f"{artifact.evidence_id}: service snapshot arrays are missing")
        return None
    service_names: set[str] = set()
    for item in services:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "status"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or not isinstance(item.get("status"), str)
            or not item["status"]
            or item["name"].casefold() in service_names
        ):
            hard_errors.append(f"{artifact.evidence_id}: service entry is invalid")
            return None
        service_names.add(item["name"].casefold())
    process_names: set[str] = set()
    for item in processes:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "process_ids"}
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or item["name"].casefold() in process_names
            or not isinstance(item.get("process_ids"), list)
            or not item["process_ids"]
            or any(
                not isinstance(pid, int) or isinstance(pid, bool) or pid < 1
                for pid in item["process_ids"]
            )
            or len(set(item["process_ids"])) != len(item["process_ids"])
        ):
            hard_errors.append(f"{artifact.evidence_id}: process entry is invalid")
            return None
        process_names.add(item["name"].casefold())
    return {
        "collector_sha256": collector_sha256,
        "services": services,
        "processes": processes,
    }


def _validate_service_state(
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    before = _one_artifact(artifacts, "SERVICE_STATE_BEFORE_JSON", hard_errors)
    after = _one_artifact(artifacts, "SERVICE_STATE_AFTER_JSON", hard_errors)
    if before is None or after is None:
        return
    before_state = _service_projection(before, "before", hard_errors, completion_errors)
    after_state = _service_projection(after, "after", hard_errors, completion_errors)
    if before_state is not None and after_state is not None and before_state != after_state:
        completion_errors.append("Docker/Ollama service or process state changed during the scan")


def _validate_gate_specific_refs(
    gate: str,
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    completion_errors: list[str],
) -> None:
    if gate == "G0":
        mappings = {
            "license_owner_confirmed": {"OWNER_ATTESTATION_JSON"},
            "third_party_boundary_clean": {"SOURCE_AUDIT_JSON"},
            "windows_ci_matrix_passed": {"CI_ATTESTATION_JSON"},
            "codeql_passed": {"CODEQL_ATTESTATION_JSON"},
            "dependency_audit_clean": {"DEPENDENCY_AUDIT_JSON"},
            "release_artifacts_validated": {"RELEASE_VALIDATION_JSON"},
            "preview_inventory_only": {
                "CI_ATTESTATION_JSON",
                "RELEASE_VALIDATION_JSON",
            },
        }
        for check_id, kinds in mappings.items():
            _require_check_kinds(
                check_id,
                kinds,
                checks,
                artifacts,
                completion_errors,
            )
        return
    if gate == "G1":
        _require_check_kinds(
            "million_resource_streaming",
            {"BENCHMARK_JSON"},
            checks,
            artifacts,
            completion_errors,
        )
        for check_id in G1_TEST_CHECKS:
            _require_check_kinds(
                check_id,
                {"TEST_REPORT_JSON"},
                checks,
                artifacts,
                completion_errors,
            )
        for check_id in G1_BOUNDARY_CHECKS:
            _require_check_kinds(
                check_id,
                {"BOUNDARY_OBSERVATION_JSON"},
                checks,
                artifacts,
                completion_errors,
            )
        return
    if gate == "G2":
        for check_id in {
            "procmon_csv_validated",
            "no_managed_root_writes",
            "no_user_asset_writes",
            "allowed_operational_writes_declared",
            "no_npm_verify",
            "no_dism",
        }:
            _require_check_kinds(
                check_id,
                {"PROCMON_VALIDATION_JSON"},
                checks,
                artifacts,
                completion_errors,
            )
        _require_check_kinds(
            "procmon_capture_complete",
            {"PROCMON_PML", "PROCMON_FILTER_SCREENSHOT"},
            checks,
            artifacts,
            completion_errors,
        )
        _require_check_kinds(
            "no_service_start",
            {"SERVICE_STATE_BEFORE_JSON", "SERVICE_STATE_AFTER_JSON"},
            checks,
            artifacts,
            completion_errors,
        )
        for check_id in {
            "inventory_smoke",
            "size_semantics_reviewed",
        }:
            _require_check_kinds(
                check_id,
                {"SCAN_REPORT_JSON"},
                checks,
                artifacts,
                completion_errors,
            )
        for check_id in {"unknown_versions_inventory_only", "preview_inventory_only"}:
            _require_check_kinds(
                check_id,
                {"SCAN_REPORT_JSON", "TEST_REPORT_JSON"},
                checks,
                artifacts,
                completion_errors,
            )
        return
    if gate != "G5":
        raise ValueError(f"unsupported gate-specific evidence contract: {gate}")
    for check_id in G5_RACE_CHECKS | {
        "identity_mismatch_skips",
        "approved_root_handle_pinned",
        "local_fixed_ntfs_only",
    }:
        _require_check_kinds(
            check_id,
            {"RACE_REPORT_JSON"},
            checks,
            artifacts,
            completion_errors,
        )
    _require_check_kinds(
        "protected_canaries_unchanged",
        {"CANARY_ATTESTATION_JSON"},
        checks,
        artifacts,
        completion_errors,
    )
    for check_id in {"deny_list_defense_in_depth", "no_fallback_delete"}:
        _require_check_kinds(
            check_id,
            {"STATIC_AUDIT_REPORT"},
            checks,
            artifacts,
            completion_errors,
        )
    _require_check_kinds(
        "independent_review",
        {"REVIEW_ATTESTATION"},
        checks,
        artifacts,
        completion_errors,
    )


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC 3339 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _validate_test_report(
    payload: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "TEST_REPORT_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "artifact_sha256",
        "source_revision",
        "started_at",
        "finished_at",
        "command",
        "exit_code",
        "checks",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("test report does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"]:
        hard_errors.append("test report artifact hash does not match the product binding")
    if report.get("source_revision") != payload["product"]["source_revision"]:
        hard_errors.append("test report source revision does not match the product binding")
    try:
        started = _parse_timestamp(report.get("started_at"), "test report started_at")
        finished = _parse_timestamp(report.get("finished_at"), "test report finished_at")
        if finished < started:
            raise ValueError("test report finished before it started")
    except ValueError as exc:
        hard_errors.append(str(exc))
    command = report.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 64
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > 4096
            or "\x00" in argument
            for argument in command
        )
    ):
        hard_errors.append("test report command argv is invalid")
    if report.get("exit_code") != 0:
        completion_errors.append("test report exit_code is not zero")
    raw_checks = report.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks or len(raw_checks) > 256:
        hard_errors.append("test report checks are missing or exceed their bound")
        return
    observed: dict[str, str] = {}
    for item in raw_checks:
        if (
            not isinstance(item, dict)
            or set(item) != {"check_id", "status", "duration_ms", "detail"}
            or not isinstance(item.get("check_id"), str)
            or not isinstance(item.get("status"), str)
            or item["status"] not in {"PASS", "FAIL", "SKIP"}
            or not isinstance(item.get("duration_ms"), int)
            or isinstance(item.get("duration_ms"), bool)
            or item["duration_ms"] < 0
            or not isinstance(item.get("detail"), str)
            or not item["detail"]
            or item["check_id"] in observed
        ):
            hard_errors.append("test report contains an invalid or duplicate check")
            return
        observed[item["check_id"]] = item["status"]
    referenced_checks = {
        check_id
        for check_id, check in checks.items()
        if artifact.evidence_id in check["evidence_refs"]
    }
    for check_id in sorted(referenced_checks):
        if observed.get(check_id) != "PASS":
            completion_errors.append(f"test report does not prove PASS for {check_id}")


def _validate_boundary_observation(
    payload: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "BOUNDARY_OBSERVATION_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "captured_at",
        "artifact_sha256",
        "source_revision",
        "observations",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("boundary observation does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"] or (
        report.get("source_revision") != payload["product"]["source_revision"]
    ):
        hard_errors.append("boundary observation is not bound to the product revision")
    try:
        _parse_timestamp(report.get("captured_at"), "boundary observation captured_at")
    except ValueError as exc:
        hard_errors.append(str(exc))
    observations = report.get("observations")
    if not isinstance(observations, list) or not observations or len(observations) > 128:
        hard_errors.append("boundary observations are missing or exceed their bound")
        return
    observed: set[str] = set()
    for item in observations:
        required = {
            "check_id",
            "fixture_label",
            "boundary_reason",
            "descended",
            "hydrated",
            "actionable_resources",
            "before_identity_digest",
            "after_identity_digest",
        }
        if (
            not isinstance(item, dict)
            or set(item) != required
            or item.get("check_id") not in G1_BOUNDARY_CHECKS
            or item["check_id"] in observed
            or not isinstance(item.get("fixture_label"), str)
            or not item["fixture_label"]
            or item.get("boundary_reason")
            not in {
                "REPARSE_POINT",
                "CLOUD_FILES_PLACEHOLDER",
                "REJECTED_ROOT",
                "ACCESS_DENIED",
                "INVENTORY_ONLY",
            }
            or item.get("descended") is not False
            or item.get("hydrated") is not False
            or item.get("actionable_resources") != 0
            or not isinstance(item.get("before_identity_digest"), str)
            or not SHA256_RE.fullmatch(item["before_identity_digest"])
            or item.get("after_identity_digest") != item["before_identity_digest"]
            or _placeholder_hash(item["before_identity_digest"])
        ):
            hard_errors.append("boundary observation contains an invalid fixture result")
            return
        observed.add(item["check_id"])
    referenced_checks = {
        check_id
        for check_id, check in checks.items()
        if artifact.evidence_id in check["evidence_refs"]
    }
    missing = sorted(referenced_checks - observed)
    if missing:
        completion_errors.append(
            "boundary observation lacks fixtures for: " + ", ".join(missing)
        )


def _validate_streaming_benchmark(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "BENCHMARK_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "evidence_kind",
        "artifact_sha256",
        "source_revision",
        "captured_at",
        "result",
        "verification",
    }
    if (
        set(report) != expected_keys
        or report.get("schema_version") != "1.0.0"
        or report.get("evidence_kind") != "G1_STREAMING_STATE_BENCHMARK"
    ):
        hard_errors.append("streaming benchmark does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"] or (
        report.get("source_revision") != payload["product"]["source_revision"]
    ):
        hard_errors.append("streaming benchmark is not bound to the product revision")
    try:
        _parse_timestamp(report.get("captured_at"), "streaming benchmark captured_at")
    except ValueError as exc:
        hard_errors.append(str(exc))
    result = report.get("result")
    verification = report.get("verification")
    if not isinstance(result, dict) or not isinstance(verification, dict):
        hard_errors.append("streaming benchmark result/verification objects are missing")
        return
    required_numeric = {
        "batch_size",
        "count",
        "database_bytes",
        "peak_python_traced_bytes",
        "peak_working_set_bytes",
        "stored",
    }
    if any(
        not isinstance(result.get(name), int)
        or isinstance(result.get(name), bool)
        or result[name] < 0
        for name in required_numeric
    ):
        hard_errors.append("streaming benchmark numeric fields are invalid")
        return
    if (
        result["count"] != 1_000_000
        or result["stored"] != 1_000_000
        or result.get("integrity") is not True
        or result["batch_size"] < 1
        or result["batch_size"] > 1024
        or result["database_bytes"] < 1
        or result["peak_python_traced_bytes"] >= 64 * 1024 * 1024
        or result["peak_working_set_bytes"] >= 512 * 1024 * 1024
        or verification.get("bounded_batching") is not True
        or verification.get("row_count_matches") is not True
        or verification.get("sqlite_integrity_check") != "ok"
    ):
        completion_errors.append("streaming benchmark does not meet the G1 acceptance bounds")


def _validate_prerequisites(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    required_by_gate = {
        "G0": set(),
        "G1": set(),
        "G2": {"G1"},
        "G5": {"G2", "G3", "G4"},
    }
    entries = payload["prerequisites"]
    by_gate: dict[str, str] = {}
    refs: set[str] = set()
    for entry in entries:
        gate = entry["gate"]
        evidence_ref = entry["evidence_ref"]
        if gate in by_gate or evidence_ref in refs:
            hard_errors.append("prerequisite gates and evidence refs must be unique")
            continue
        by_gate[gate] = evidence_ref
        refs.add(evidence_ref)
    expected = required_by_gate[payload["gate"]]
    missing = sorted(expected - by_gate.keys())
    unexpected = sorted(by_gate.keys() - expected)
    if missing:
        completion_errors.append("missing prerequisite gate results: " + ", ".join(missing))
    if unexpected:
        hard_errors.append("unexpected prerequisite gate results: " + ", ".join(unexpected))

    expected_binding = {
        "version": payload["product"]["version"],
        "source_revision": payload["product"]["source_revision"],
        "artifact_sha256": payload["product"]["artifact_sha256"],
    }
    for gate in sorted(expected & by_gate.keys()):
        evidence_ref = by_gate[gate]
        artifact = artifacts.get(evidence_ref)
        if artifact is None:
            hard_errors.append(f"{gate} prerequisite references missing evidence")
            continue
        if artifact.kind != "GATE_RESULT_JSON":
            hard_errors.append(f"{gate} prerequisite is not a GATE_RESULT_JSON")
            continue
        result = _read_json_artifact(artifact, hard_errors)
        if result is None:
            continue
        target_gate = result.get("matrix") or result.get("gate")
        if (
            result.get("schema_version") != "1.0.0"
            or result.get("verdict") != "PASS"
            or target_gate != gate
            or result.get("matrix_errors") not in (None, [])
        ):
            completion_errors.append(f"{gate} prerequisite result is not a clean PASS")
        if result.get("product_binding") != expected_binding:
            hard_errors.append(f"{gate} prerequisite product binding differs from this run")
        manifests = result.get("manifests")
        if (
            not isinstance(manifests, list)
            or not manifests
            or any(
                not isinstance(item, dict)
                or item.get("passed") is not True
                or item.get("hard_errors") != []
                or item.get("completion_errors") != []
                for item in manifests
            )
        ):
            completion_errors.append(f"{gate} prerequisite has incomplete manifest results")


def _validate_race_report(
    payload: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "RACE_REPORT_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "artifact_sha256",
        "source_revision",
        "captured_at",
        "filesystem",
        "volume_kind",
        "races",
        "unexpected_mutations",
        "action_scope_expansions",
        "result",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("race report does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"] or (
        report.get("source_revision") != payload["product"]["source_revision"]
    ):
        hard_errors.append("race report is not bound to the product revision")
    try:
        _parse_timestamp(report.get("captured_at"), "race report captured_at")
    except ValueError as exc:
        hard_errors.append(str(exc))
    if report.get("filesystem") != "NTFS" or report.get("volume_kind") != "FIXED":
        completion_errors.append("race report did not run on a fixed NTFS volume")
    top_unexpected = report.get("unexpected_mutations")
    top_expansions = report.get("action_scope_expansions")
    if (
        not isinstance(top_unexpected, int)
        or isinstance(top_unexpected, bool)
        or top_unexpected != 0
        or not isinstance(top_expansions, int)
        or isinstance(top_expansions, bool)
        or top_expansions != 0
        or report.get("result") != "PASS"
    ):
        completion_errors.append("race report contains a mutation, scope expansion, or failure")
    races = report.get("races")
    if not isinstance(races, list) or len(races) != len(G5_RACE_CHECKS):
        hard_errors.append("race report must contain exactly the seven required races")
        return
    observed: set[str] = set()
    for item in races:
        expected_race_keys = {
            "check_id",
            "attempts",
            "barrier_hits",
            "safe_skips",
            "identity_mismatch_skips",
            "lock_skips",
            "unexpected_mutations",
            "action_scope_expansions",
            "seed_commitment_sha256",
            "protected_canary_before_sha256",
            "protected_canary_after_sha256",
            "duration_ms",
            "result",
        }
        if not isinstance(item, dict) or set(item) != expected_race_keys:
            hard_errors.append("race report entry does not match the closed contract")
            return
        check_id = item.get("check_id")
        numeric_fields = {
            "attempts",
            "barrier_hits",
            "safe_skips",
            "identity_mismatch_skips",
            "lock_skips",
            "unexpected_mutations",
            "action_scope_expansions",
            "duration_ms",
        }
        if (
            check_id not in G5_RACE_CHECKS
            or check_id in observed
            or any(
                not isinstance(item.get(name), int)
                or isinstance(item.get(name), bool)
                or item[name] < 0
                for name in numeric_fields
            )
            or item["attempts"] < 10_000
            or item["barrier_hits"] < 10_000
            or item["barrier_hits"] > item["attempts"]
            or item["safe_skips"] < item["barrier_hits"]
            or item["unexpected_mutations"] != 0
            or item["action_scope_expansions"] != 0
            or not isinstance(item.get("seed_commitment_sha256"), str)
            or not SHA256_RE.fullmatch(item["seed_commitment_sha256"])
            or _placeholder_hash(item["seed_commitment_sha256"])
            or not isinstance(item.get("protected_canary_before_sha256"), str)
            or not SHA256_RE.fullmatch(item["protected_canary_before_sha256"])
            or item.get("protected_canary_after_sha256")
            != item["protected_canary_before_sha256"]
            or _placeholder_hash(item["protected_canary_before_sha256"])
            or item.get("result") != "PASS"
        ):
            completion_errors.append(f"race report does not prove a safe 10,000-hit {check_id}")
            continue
        if checks[check_id].get("iterations") != item["barrier_hits"]:
            hard_errors.append(f"{check_id} manifest iterations differ from barrier hits")
        observed.add(check_id)
    missing = sorted(G5_RACE_CHECKS - observed)
    if missing:
        completion_errors.append("race report lacks passing entries for: " + ", ".join(missing))


def _validate_canary_attestation(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "CANARY_ATTESTATION_JSON", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "artifact_sha256",
        "source_revision",
        "captured_at",
        "groups",
        "unexpected_mutations",
        "result",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("canary attestation does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"] or (
        report.get("source_revision") != payload["product"]["source_revision"]
    ):
        hard_errors.append("canary attestation is not bound to the product revision")
    try:
        _parse_timestamp(report.get("captured_at"), "canary attestation captured_at")
    except ValueError as exc:
        hard_errors.append(str(exc))
    groups = report.get("groups")
    if not isinstance(groups, list) or not groups or len(groups) > 128:
        hard_errors.append("canary attestation groups are missing or exceed their bound")
        return
    observed: set[str] = set()
    for group in groups:
        if (
            not isinstance(group, dict)
            or set(group)
            != {
                "label",
                "object_count",
                "before_digest",
                "after_digest",
                "file_identities_verified",
                "unexpected_mutations",
            }
            or not isinstance(group.get("label"), str)
            or group["label"] in observed
            or not isinstance(group.get("object_count"), int)
            or isinstance(group.get("object_count"), bool)
            or group["object_count"] < 1
            or not isinstance(group.get("before_digest"), str)
            or not SHA256_RE.fullmatch(group["before_digest"])
            or group.get("after_digest") != group["before_digest"]
            or _placeholder_hash(group["before_digest"])
            or group.get("file_identities_verified") is not True
            or not isinstance(group.get("unexpected_mutations"), int)
            or isinstance(group.get("unexpected_mutations"), bool)
            or group["unexpected_mutations"] != 0
        ):
            hard_errors.append("canary attestation contains an invalid group")
            return
        observed.add(group["label"])
    missing = sorted(set(payload["scope"]["protected_asset_labels"]) - observed)
    if missing:
        completion_errors.append("canary attestation lacks protected groups: " + ", ".join(missing))
    unexpected = report.get("unexpected_mutations")
    if (
        not isinstance(unexpected, int)
        or isinstance(unexpected, bool)
        or unexpected != 0
        or report.get("result") != "PASS"
    ):
        completion_errors.append("canary attestation reports mutation or failure")


def _validate_static_audit(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "STATIC_AUDIT_REPORT", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "artifact_sha256",
        "source_revision",
        "reviewed_at",
        "mutation_apis",
        "prohibited_api_hits",
        "approved_root_handle_pinned",
        "fail_closed_branches_reviewed",
        "no_fallback_delete",
        "deny_list_version",
        "deny_list_patterns",
        "conclusion",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("static audit does not match the closed 1.0.0 contract")
        return
    if report.get("artifact_sha256") != payload["product"]["artifact_sha256"] or (
        report.get("source_revision") != payload["product"]["source_revision"]
    ):
        hard_errors.append("static audit is not bound to the product revision")
    try:
        _parse_timestamp(report.get("reviewed_at"), "static audit reviewed_at")
    except ValueError as exc:
        hard_errors.append(str(exc))
    prohibited = report.get("prohibited_api_hits")
    required_prohibited = {
        "shutil.rmtree",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "DeleteFileW",
        "shell_delete",
        "string_path_fallback",
    }
    patterns = report.get("deny_list_patterns")
    normalized_patterns = (
        {str(value).casefold() for value in patterns} if isinstance(patterns, list) else set()
    )
    required_patterns = {
        ".git",
        "lockfile",
        ".env*",
        "*.key",
        "*.pem",
        "globalstorage",
        "local history",
        ".codex",
        ".claude",
    }
    if (
        not isinstance(report.get("mutation_apis"), list)
        or not report["mutation_apis"]
        or len(report["mutation_apis"]) > 128
        or any(
            not isinstance(value, str) or not value or len(value) > 512
            for value in report["mutation_apis"]
        )
        or not isinstance(prohibited, dict)
        or set(prohibited) != required_prohibited
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value != 0
            for value in prohibited.values()
        )
        or report.get("approved_root_handle_pinned") is not True
        or report.get("fail_closed_branches_reviewed") is not True
        or report.get("no_fallback_delete") is not True
        or not isinstance(report.get("deny_list_version"), str)
        or not report["deny_list_version"]
        or not isinstance(patterns, list)
        or any(not isinstance(value, str) or not value for value in patterns)
        or not required_patterns <= normalized_patterns
        or report.get("conclusion") != "PASS"
    ):
        completion_errors.append("static audit does not prove the G5 executor invariants")


def _validate_review_attestation(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    artifact = _one_artifact(artifacts, "REVIEW_ATTESTATION", hard_errors)
    if artifact is None:
        return
    report = _read_json_artifact(artifact, hard_errors)
    if report is None:
        return
    expected_keys = {
        "schema_version",
        "artifact_sha256",
        "source_revision",
        "author_ids",
        "reviewer_ids",
        "review_method",
        "reviewed_at",
        "open_findings",
        "conclusion",
    }
    if set(report) != expected_keys or report.get("schema_version") != "1.0.0":
        hard_errors.append("review attestation does not match the closed 1.0.0 contract")
        return
    review = payload["review"]
    if (
        report.get("artifact_sha256") != payload["product"]["artifact_sha256"]
        or report.get("source_revision") != payload["product"]["source_revision"]
        or report.get("author_ids") != review["author_ids"]
        or report.get("reviewer_ids") != review["reviewer_ids"]
        or report.get("review_method") != review["review_method"]
        or report.get("reviewed_at") != review["reviewed_at"]
    ):
        hard_errors.append("review attestation does not match manifest/product binding")
    if report.get("open_findings") != [] or report.get("conclusion") != "PASS":
        completion_errors.append("independent review has open findings or did not pass")


def _validate_g0_artifacts(
    payload: dict[str, Any],
    artifacts: dict[str, Artifact],
    hard_errors: list[str],
    completion_errors: list[str],
) -> None:
    owner_artifact = _one_artifact(artifacts, "OWNER_ATTESTATION_JSON", hard_errors)
    source_artifact = _one_artifact(artifacts, "SOURCE_AUDIT_JSON", hard_errors)
    ci_artifact = _one_artifact(artifacts, "CI_ATTESTATION_JSON", hard_errors)
    codeql_artifact = _one_artifact(artifacts, "CODEQL_ATTESTATION_JSON", hard_errors)
    audit_artifact = _one_artifact(artifacts, "DEPENDENCY_AUDIT_JSON", hard_errors)
    release_artifact = _one_artifact(artifacts, "RELEASE_VALIDATION_JSON", hard_errors)

    if owner_artifact is not None:
        owner = _read_json_artifact(owner_artifact, hard_errors)
        expected = {
            "schema_version",
            "source_revision",
            "decision_at",
            "owner_id",
            "license_expression",
            "approved",
            "no_third_party_rule_copy_attested",
            "notes",
        }
        if owner is None:
            pass
        elif set(owner) != expected or owner.get("schema_version") != "1.0.0":
            hard_errors.append("owner attestation does not match the closed contract")
        else:
            try:
                _parse_timestamp(owner.get("decision_at"), "owner decision_at")
            except ValueError as exc:
                hard_errors.append(str(exc))
            if (
                owner.get("source_revision") != payload["product"]["source_revision"]
                or owner.get("owner_id") not in payload["review"]["author_ids"]
            ):
                hard_errors.append("owner attestation does not match manifest/revision")
            if (
                owner.get("license_expression") != "GPL-3.0-or-later"
                or owner.get("approved") is not True
                or owner.get("no_third_party_rule_copy_attested") is not True
            ):
                completion_errors.append("project owner has not approved the G0 license boundary")

    if source_artifact is not None:
        source = _read_json_artifact(source_artifact, hard_errors)
        if source is not None:
            required_keys = {
                "schema_version",
                "evidence_kind",
                "captured_at",
                "source_revision",
                "source_tree_sha256",
                "auditor_sha256",
                "checked_file_count",
                "checked_total_bytes",
                "license_sha256",
                "third_party_notices_sha256",
                "pyproject_sha256",
                "runtime_dependencies",
                "declared_license_expression",
                "declared_license_files",
                "console_scripts",
                "runtime_plugin_groups",
                "prohibited_vendored_paths",
                "mechanical_result",
                "owner_license_decision_proven",
                "originality_proven",
                "limitations",
            }
            hashes = {
                source.get("source_tree_sha256"),
                source.get("auditor_sha256"),
                source.get("license_sha256"),
                source.get("third_party_notices_sha256"),
                source.get("pyproject_sha256"),
            }
            if (
                set(source) != required_keys
                or source.get("schema_version") != "1.0.0"
                or source.get("evidence_kind") != "G0_SOURCE_BOUNDARY_AUDIT"
                or source.get("source_revision") != payload["product"]["source_revision"]
                or any(
                    not isinstance(value, str)
                    or not SHA256_RE.fullmatch(value)
                    or _placeholder_hash(value)
                    for value in hashes
                )
            ):
                hard_errors.append("source audit does not match the closed revision contract")
            try:
                _parse_timestamp(source.get("captured_at"), "source audit captured_at")
            except ValueError as exc:
                hard_errors.append(str(exc))
            if (
                not isinstance(source.get("checked_file_count"), int)
                or isinstance(source.get("checked_file_count"), bool)
                or source["checked_file_count"] < 1
                or not isinstance(source.get("checked_total_bytes"), int)
                or isinstance(source.get("checked_total_bytes"), bool)
                or source["checked_total_bytes"] < 1
            ):
                hard_errors.append("source audit file/byte counts are invalid")
            if (
                source.get("mechanical_result") != "PASS"
                or source.get("license_sha256") != CANONICAL_GPLV3_SHA256
                or source.get("runtime_dependencies") != []
                or source.get("declared_license_expression") != "GPL-3.0-or-later"
                or source.get("declared_license_files")
                != ["LICENSE", "THIRD_PARTY_NOTICES.md"]
                or source.get("runtime_plugin_groups") != []
                or source.get("prohibited_vendored_paths") != []
                or source.get("console_scripts")
                != {"DevClean": "devclean.cli.main:main"}
                or source.get("owner_license_decision_proven") is not False
                or source.get("originality_proven") is not False
            ):
                completion_errors.append("source audit does not prove the mechanical G0 boundary")

    if audit_artifact is not None:
        audit = _read_json_artifact(audit_artifact, hard_errors)
        if audit is not None:
            dependencies = audit.get("dependencies")
            fixes = audit.get("fixes")
            if not isinstance(dependencies, list) or not dependencies:
                hard_errors.append("dependency audit has no dependency result array")
            elif any(
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or ("vulns" in item and item.get("vulns") != [])
                or ("vulns" not in item and "skip_reason" not in item)
                for item in dependencies
            ):
                completion_errors.append("dependency audit contains vulnerabilities/malformed rows")
            if fixes != []:
                completion_errors.append("dependency audit contains unresolved fixes")

    release: dict[str, Any] | None = None
    if release_artifact is not None:
        release = _read_json_artifact(release_artifact, hard_errors)
        if release is not None:
            expected_release_keys = {
                "schema_version",
                "source_revision",
                "version",
                "captured_at",
                "artifact_sha256",
                "wheel_sha256",
                "sbom_sha256",
                "checksums_sha256",
                "builder_sha256",
                "validator_sha256",
                "uv_lock_sha256",
                "clean_runtime_install",
                "wheel_reproducible",
                "sbom_reproducible",
                "schemas_validated",
                "wheel_record_validated",
                "inventory_only_surface_validated",
                "result",
            }
            digest_fields = {
                release.get("artifact_sha256"),
                release.get("wheel_sha256"),
                release.get("sbom_sha256"),
                release.get("checksums_sha256"),
                release.get("builder_sha256"),
                release.get("validator_sha256"),
                release.get("uv_lock_sha256"),
            }
            if (
                set(release) != expected_release_keys
                or release.get("schema_version") != "1.0.0"
                or any(
                    not isinstance(value, str)
                    or not SHA256_RE.fullmatch(value)
                    or _placeholder_hash(value)
                    for value in digest_fields
                )
                or release.get("source_revision") != payload["product"]["source_revision"]
                or release.get("version") != payload["product"]["version"]
                or release.get("artifact_sha256") != payload["product"]["artifact_sha256"]
                or release.get("wheel_sha256") != payload["product"]["artifact_sha256"]
            ):
                hard_errors.append("release validation does not match product/revision hashes")
            try:
                _parse_timestamp(release.get("captured_at"), "release validation captured_at")
            except ValueError as exc:
                hard_errors.append(str(exc))
            required_true = {
                "clean_runtime_install",
                "wheel_reproducible",
                "sbom_reproducible",
                "schemas_validated",
                "wheel_record_validated",
                "inventory_only_surface_validated",
            }
            if any(release.get(name) is not True for name in required_true) or (
                release.get("result") != "PASS"
            ):
                completion_errors.append("release artifact validation is incomplete")

    if ci_artifact is not None:
        ci = _read_json_artifact(ci_artifact, hard_errors)
        if ci is not None:
            expected_ci_keys = {
                "schema_version",
                "source_revision",
                "artifact_sha256",
                "captured_at",
                "workflow_sha256",
                "run_id",
                "run_attempt",
                "repository",
                "python_matrix",
                "actions_pinned",
                "persist_credentials_false",
                "inventory_only_contract_tests_passed",
                "dependency_audit_sha256",
                "release_validation_sha256",
                "conclusion",
            }
            if set(ci) != expected_ci_keys or ci.get("schema_version") != "1.0.0":
                hard_errors.append("CI attestation does not match the closed contract")
            else:
                try:
                    _parse_timestamp(ci.get("captured_at"), "CI attestation captured_at")
                except ValueError as exc:
                    hard_errors.append(str(exc))
                matrix = ci.get("python_matrix")
                versions: set[str] = set()
                if not isinstance(matrix, list) or len(matrix) != 3:
                    hard_errors.append("CI attestation Python matrix is invalid")
                else:
                    for row in matrix:
                        row_keys = {
                            "python_version",
                            "conclusion",
                            "tests_passed",
                            "tests_skipped",
                            "coverage_percent",
                            "schemas_validated",
                            "dependency_audit_clean",
                            "ruff_passed",
                            "mypy_passed",
                        }
                        if not isinstance(row, dict) or set(row) != row_keys:
                            hard_errors.append("CI matrix row does not match the closed contract")
                            continue
                        versions.add(str(row["python_version"]))
                        if (
                            row.get("conclusion") != "PASS"
                            or not isinstance(row.get("tests_passed"), int)
                            or isinstance(row.get("tests_passed"), bool)
                            or row["tests_passed"] < 1
                            or not isinstance(row.get("tests_skipped"), int)
                            or isinstance(row.get("tests_skipped"), bool)
                            or row["tests_skipped"] < 0
                            or not isinstance(row.get("coverage_percent"), (int, float))
                            or isinstance(row.get("coverage_percent"), bool)
                            or row["coverage_percent"] < 75
                            or any(
                                row.get(name) is not True
                                for name in {
                                    "schemas_validated",
                                    "dependency_audit_clean",
                                    "ruff_passed",
                                    "mypy_passed",
                                }
                            )
                        ):
                            completion_errors.append("one or more CI matrix jobs did not pass")
                    if versions != {"3.11", "3.12", "3.13"}:
                        completion_errors.append("CI Python matrix does not cover 3.11-3.13")
                if (
                    ci.get("source_revision") != payload["product"]["source_revision"]
                    or ci.get("artifact_sha256") != payload["product"]["artifact_sha256"]
                    or not isinstance(ci.get("workflow_sha256"), str)
                    or not SHA256_RE.fullmatch(ci["workflow_sha256"])
                    or _placeholder_hash(ci["workflow_sha256"])
                    or not isinstance(ci.get("run_id"), str)
                    or not ci["run_id"].isascii()
                    or not ci["run_id"].isdecimal()
                    or not isinstance(ci.get("run_attempt"), int)
                    or isinstance(ci.get("run_attempt"), bool)
                    or ci["run_attempt"] < 1
                    or not isinstance(ci.get("repository"), str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", ci["repository"]
                    )
                    or ci.get("actions_pinned") is not True
                    or ci.get("persist_credentials_false") is not True
                    or ci.get("inventory_only_contract_tests_passed") is not True
                    or ci.get("conclusion") != "PASS"
                ):
                    completion_errors.append("CI attestation does not prove the G0 workflow")
                if audit_artifact is not None and ci.get(
                    "dependency_audit_sha256"
                ) != audit_artifact.sha256:
                    hard_errors.append("CI attestation is not bound to dependency audit evidence")
                if release_artifact is not None and ci.get(
                    "release_validation_sha256"
                ) != release_artifact.sha256:
                    hard_errors.append("CI attestation is not bound to release validation evidence")

    if codeql_artifact is not None:
        codeql = _read_json_artifact(codeql_artifact, hard_errors)
        expected_codeql_keys = {
            "schema_version",
            "source_revision",
            "captured_at",
            "workflow_sha256",
            "run_id",
            "language",
            "build_mode",
            "query_suite",
            "unresolved_error_alerts",
            "conclusion",
        }
        if codeql is None:
            pass
        elif set(codeql) != expected_codeql_keys or codeql.get("schema_version") != "1.0.0":
            hard_errors.append("CodeQL attestation does not match the closed contract")
        else:
            try:
                _parse_timestamp(codeql.get("captured_at"), "CodeQL attestation captured_at")
            except ValueError as exc:
                hard_errors.append(str(exc))
            unresolved = codeql.get("unresolved_error_alerts")
            if (
                codeql.get("source_revision") != payload["product"]["source_revision"]
                or not isinstance(codeql.get("workflow_sha256"), str)
                or not SHA256_RE.fullmatch(codeql["workflow_sha256"])
                or _placeholder_hash(codeql["workflow_sha256"])
                or not isinstance(codeql.get("run_id"), str)
                or not codeql["run_id"].isascii()
                or not codeql["run_id"].isdecimal()
                or codeql.get("language") != "python"
                or codeql.get("build_mode") != "none"
                or codeql.get("query_suite") != "security-extended"
                or not isinstance(unresolved, int)
                or isinstance(unresolved, bool)
                or unresolved != 0
                or codeql.get("conclusion") != "PASS"
            ):
                completion_errors.append("CodeQL attestation does not prove a clean Python run")


def _placeholder_hash(value: str) -> bool:
    return len(set(value)) <= 1


def _validate_completion(
    payload: dict[str, Any],
    checks: dict[str, dict[str, Any]],
    artifacts: dict[str, Artifact],
    completion_errors: list[str],
) -> None:
    gate = payload["gate"]
    missing_kinds = REQUIRED_EVIDENCE_KINDS[gate] - {
        artifact.kind for artifact in artifacts.values()
    }
    if missing_kinds:
        completion_errors.append(
            f"missing required evidence kinds: {', '.join(sorted(missing_kinds))}"
        )
    for check_id in sorted(REQUIRED_CHECKS[gate]):
        check = checks[check_id]
        if check["status"] != "PASS":
            completion_errors.append(f"required check is not PASS: {check_id}")
        if not check["evidence_refs"]:
            completion_errors.append(f"required check has no evidence refs: {check_id}")
    if any(check["status"] == "FAIL" for check in checks.values()):
        completion_errors.append("one or more checks explicitly failed")
    environment = payload["environment"]
    if gate in {"G1", "G2", "G5"} and environment["user_integrity"] != "STANDARD":
        completion_errors.append("gate runs must use a standard-integrity DevClean process")
    if gate in {"G1", "G2", "G5"}:
        if not environment["os_product"].casefold().startswith("windows 11"):
            completion_errors.append("gate runs must use Windows 11")
        if environment["architecture"] != "x86_64":
            completion_errors.append("v0.x gate runs must use the supported x86_64 architecture")
    scope = payload["scope"]
    process_names = [name.casefold() for name in scope["required_process_names"]]
    if len(process_names) != len(set(process_names)):
        completion_errors.append("required process names collide case-insensitively")
    all_root_labels = (
        list(scope["managed_root_labels"])
        + list(scope["protected_asset_labels"])
        + list(scope["allowed_write_root_labels"])
    )
    if len(all_root_labels) != len(set(all_root_labels)):
        completion_errors.append("managed/protected/allowed root labels must be disjoint")
    if gate == "G2":
        if set(scope["adapters"]) != G2_ADAPTERS:
            completion_errors.append("G2 scope must contain exactly the nine built-in adapters")
        if not scope["managed_root_labels"]:
            completion_errors.append("G2 needs at least one managed root label")
        required_processes = {name.casefold() for name in scope["required_process_names"]}
        missing_processes = sorted(
            process_name
            for adapter_id, process_name in G2_REQUIRED_PROCESS_BY_ADAPTER.items()
            if adapter_id in scope["available_adapters"]
            and process_name not in required_processes
        )
        if missing_processes:
            completion_errors.append(
                "G2 required_process_names omit AVAILABLE adapter processes: "
                + ", ".join(missing_processes)
            )
        if not scope["protected_asset_labels"]:
            completion_errors.append("G2 needs at least one protected asset label")
        if not scope["allowed_write_root_labels"]:
            completion_errors.append("G2 needs at least one allowed-write root label")
    if gate == "G0":
        if environment["machine_kind"] != "CI_RUNNER":
            completion_errors.append("G0 CI evidence must identify a CI runner")
        if set(scope["adapters"]) != {"repository"} or set(
            scope["available_adapters"]
        ) != {"repository"}:
            completion_errors.append("G0 scope must be the repository release boundary")
    if gate == "G5":
        if environment["machine_kind"] != "DISPOSABLE_VM":
            completion_errors.append("G5 race execution must use a disposable VM")
        if environment["filesystem"] != "NTFS" or environment["volume_kind"] != "FIXED":
            completion_errors.append("G5 is restricted to a local fixed NTFS volume")
        for check_id in sorted(G5_RACE_CHECKS):
            if checks[check_id].get("iterations", 0) < 10_000:
                completion_errors.append(f"{check_id} has fewer than 10,000 iterations")
        review = payload["review"]
        if review["review_method"] == "NONE" or not review["reviewer_ids"]:
            completion_errors.append("G5 needs at least one independent reviewer")
        if set(review["author_ids"]) & set(review["reviewer_ids"]):
            completion_errors.append("G5 author and reviewer identities overlap")
        if review["reviewed_at"] is None:
            completion_errors.append("G5 review timestamp is missing")
    if _placeholder_hash(payload["product"]["artifact_sha256"]):
        completion_errors.append("product artifact hash is a placeholder")
    if _placeholder_hash(environment["machine_fingerprint_sha256"]):
        completion_errors.append("machine fingerprint hash is a placeholder")
    revision_text = payload["product"]["source_revision"].casefold()
    if any(
        token in revision_text
        for token in {"replace", "placeholder", "uncommitted", "unknown", "worktree"}
    ):
        completion_errors.append("source revision is a placeholder")
    if not GIT_REVISION_RE.fullmatch(payload["product"]["source_revision"]):
        completion_errors.append(
            "source revision must be a full 40- or 64-character lowercase Git object ID"
        )
    _validate_product_binding(payload, artifacts, completion_errors)


def validate_manifest(
    manifest_path: Path, validator: Draft202012Validator
) -> ManifestResult:
    manifest_sha256 = "unreadable"
    hard_errors: list[str] = []
    completion_errors: list[str] = []
    try:
        manifest_path = Path(os.path.abspath(manifest_path))
        _reject_reparse_chain(manifest_path)
        if manifest_path.stat(follow_symlinks=False).st_nlink != 1:
            raise ValueError("manifest must not have multiple hard links")
        content = _read_stable_bytes(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
        manifest_sha256 = hashlib.sha256(content).hexdigest()
        raw = content.decode("utf-8", errors="strict")
        payload = strict_json_loads(raw)
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return ManifestResult(manifest_sha256, None, [f"invalid manifest JSON: {exc}"], [])
    if not isinstance(payload, dict):
        return ManifestResult(manifest_sha256, None, ["manifest root must be an object"], [])
    schema_errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if schema_errors:
        return ManifestResult(
            manifest_sha256,
            payload,
            [_schema_error_message(error) for error in schema_errors],
            [],
        )

    artifacts = _verify_artifacts(manifest_path, payload, hard_errors)
    checks = _check_references(payload, artifacts, hard_errors)
    gate = payload["gate"]
    missing_checks = sorted(REQUIRED_CHECKS[gate] - checks.keys())
    if missing_checks:
        hard_errors.append(f"missing required checks: {', '.join(missing_checks)}")
    for check_id in REQUIRED_CHECKS[gate] & checks.keys():
        if checks[check_id]["required"] is not True:
            hard_errors.append(f"gate-required check is marked optional: {check_id}")
    if missing_checks:
        return ManifestResult(manifest_sha256, payload, hard_errors, completion_errors)

    _validate_gate_specific_refs(gate, checks, artifacts, completion_errors)
    _validate_prerequisites(payload, artifacts, hard_errors, completion_errors)
    if gate == "G0":
        _validate_g0_artifacts(
            payload,
            artifacts,
            hard_errors,
            completion_errors,
        )
    if gate in {"G1", "G2"}:
        _validate_test_report(
            payload,
            checks,
            artifacts,
            hard_errors,
            completion_errors,
        )
    if gate == "G1":
        _validate_boundary_observation(
            payload,
            checks,
            artifacts,
            hard_errors,
            completion_errors,
        )
        _validate_streaming_benchmark(
            payload,
            artifacts,
            hard_errors,
            completion_errors,
        )
    if gate == "G2":
        _validate_procmon_binding(payload, artifacts, hard_errors, completion_errors)
        _validate_scan_report(payload, artifacts, hard_errors, completion_errors)
        _validate_service_state(artifacts, hard_errors, completion_errors)
    if gate == "G5":
        _validate_race_report(
            payload,
            checks,
            artifacts,
            hard_errors,
            completion_errors,
        )
        _validate_canary_attestation(
            payload,
            artifacts,
            hard_errors,
            completion_errors,
        )
        _validate_static_audit(
            payload,
            artifacts,
            hard_errors,
            completion_errors,
        )
        _validate_review_attestation(
            payload,
            artifacts,
            hard_errors,
            completion_errors,
        )
    _validate_completion(payload, checks, artifacts, completion_errors)

    conclusion = payload["conclusion"]
    if conclusion == "PASS" and completion_errors:
        hard_errors.append("manifest claims PASS but completion requirements are unmet")
    if conclusion == "FAIL" and not any(
        check["status"] == "FAIL" for check in checks.values()
    ):
        hard_errors.append("manifest claims FAIL but no check is marked FAIL")
    return ManifestResult(manifest_sha256, payload, hard_errors, completion_errors)


def validate_matrix(gate: str, results: Sequence[ManifestResult]) -> list[str]:
    errors: list[str] = []
    if any(result.gate != gate for result in results):
        errors.append(f"all matrix manifests must target {gate}")
        return errors
    if any(not result.passed for result in results):
        errors.append("every matrix manifest must be an individually complete PASS")
        return errors
    payloads = [result.payload for result in results if result.payload is not None]
    run_ids = [payload["run_id"] for payload in payloads]
    fingerprints = [payload["environment"]["machine_fingerprint_sha256"] for payload in payloads]
    if len(run_ids) != len(set(run_ids)):
        errors.append("matrix run_id values must be unique")
    if len(fingerprints) != len(set(fingerprints)):
        errors.append("matrix machine fingerprints must be unique")
    product_bindings = {
        (
            payload["product"]["version"],
            payload["product"]["source_revision"],
            payload["product"]["artifact_sha256"],
        )
        for payload in payloads
    }
    if len(product_bindings) != 1:
        errors.append("matrix manifests are not bound to the same product artifact")
    kinds = [payload["environment"]["machine_kind"] for payload in payloads]
    if gate == "G2":
        if kinds.count("PHYSICAL") < 2:
            errors.append("G2 requires at least two physical machines")
        if kinds.count("DISPOSABLE_VM") < 1:
            errors.append("G2 requires at least one disposable VM")
        if len(payloads) < 3:
            errors.append("G2 requires at least three individually complete manifests")
        locales = {payload["environment"]["locale"].casefold() for payload in payloads}
        if not {"en-us", "zh-cn"}.issubset(locales):
            errors.append("G2 requires both en-US and zh-CN locale coverage")
        available_union = {
            adapter
            for payload in payloads
            for adapter in payload["scope"]["available_adapters"]
        }
        if available_union != G2_ADAPTERS:
            missing = sorted(G2_ADAPTERS - available_union)
            errors.append(
                "G2 machine matrix lacks real AVAILABLE coverage for adapters: "
                + ", ".join(missing)
            )
    elif gate == "G1" and kinds.count("PHYSICAL") < 1:
        errors.append("G1 final acceptance requires at least one physical machine")
    return errors


def _result_payload(
    results: Sequence[ManifestResult], matrix: str | None, matrix_errors: Sequence[str]
) -> dict[str, Any]:
    bindings = {
        (
            result.payload["product"]["version"],
            result.payload["product"]["source_revision"],
            result.payload["product"]["artifact_sha256"],
        )
        for result in results
        if result.payload is not None
    }
    product_binding = None
    if len(bindings) == 1:
        version, source_revision, artifact_sha256 = next(iter(bindings))
        product_binding = {
            "version": version,
            "source_revision": source_revision,
            "artifact_sha256": artifact_sha256,
        }
    return {
        "schema_version": "1.0.0",
        "verdict": "PASS"
        if all(result.passed for result in results) and not matrix_errors
        else "FAIL",
        "matrix": matrix,
        "product_binding": product_binding,
        "matrix_errors": list(matrix_errors),
        "manifests": [
            {
                "manifest_sha256": result.manifest_sha256,
                "gate": result.gate,
                "run_id": result.run_id,
                "passed": result.passed,
                "hard_errors": result.hard_errors,
                "completion_errors": result.completion_errors,
            }
            for result in results
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--matrix", choices=("G1", "G2", "G5"))
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="lint templates without claiming that a gate passed",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validator = _schema_validator(args.schema)
        results = [validate_manifest(path, validator) for path in args.manifests]
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Gate evidence validation error: {exc}", file=sys.stderr)
        return 2
    matrix_errors = validate_matrix(args.matrix, results) if args.matrix else []
    payload = _result_payload(results, args.matrix, matrix_errors)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        if args.output.exists():
            print(
                f"Gate evidence validation error: refusing to overwrite {args.output}",
                file=sys.stderr,
            )
            return 2
        if not args.output.parent.is_dir():
            print(
                "Gate evidence validation error: output parent does not exist",
                file=sys.stderr,
            )
            return 2
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(rendered)
    if args.allow_incomplete:
        return 0 if all(not result.hard_errors for result in results) else 1
    return 0 if payload["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
