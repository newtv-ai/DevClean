"""Non-executable scan reports.

Reports are one-way exports. They are never accepted as plan input.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from devclean.core.models import SCHEMA_VERSION, utc_now
from devclean.core.state import StateStore
from devclean.evidence.redaction import redact_full_path, redact_secrets, redact_text
from devclean.platform.windows.volumes import is_local_fixed_path

_SAFE_INTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_FILE_ID = re.compile(r"^[a-f0-9]{1,32}$")
_FILE_ID_KIND = re.compile(r"^[a-z0-9_]{1,32}$")
_VOLUME_SERIAL = re.compile(r"^[a-f0-9]{1,16}$")
_EVIDENCE_FILE = re.compile(
    r"^(?:commands|loopback)/[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\."
    r"(?:stdout|stderr|response)\.redacted\.txt$"
)
_INTERNAL_ID_FIELDS = frozenset(
    {"action_id", "candidate_id", "evidence_id", "plan_id", "run_id", "scan_id"}
)
_INTERNAL_ID_LIST_FIELDS = frozenset({"evidence_ids"})
_EVIDENCE_FILE_FIELDS = frozenset({"response_file", "stderr_file", "stdout_file"})


def build_report(store: StateStore, scan_id: str, *, redact: bool = True) -> dict[str, Any]:
    scan = store.get_scan(scan_id)
    if scan is None:
        raise KeyError(f"unknown scan: {scan_id}")
    store.validate_evidence_references(scan_id)

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now().isoformat(),
        "scan": scan,
        "resources": store.list_resources(scan_id),
        "errors": store.list_scan_errors(scan_id),
        "adapter_runs": list(store.iter_adapter_runs(scan_id)),
        "evidence": list(store.iter_evidence(scan_id)),
        "safety_boundary": {
            "executable": False,
            "statement": (
                "This report cannot be imported or executed. Arbitrary scan results are "
                "report-only."
            ),
        },
    }
    return cast(dict[str, Any], _redact(report, redact_paths=redact))


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def iter_json_report(store: StateStore, scan_id: str, *, redact: bool = True) -> Iterator[str]:
    """Yield a valid JSON report without materializing all resources in memory."""

    scan = store.get_scan(scan_id)
    if scan is None:
        raise KeyError(f"unknown scan: {scan_id}")
    store.validate_evidence_references(scan_id)
    generated_at = utc_now().isoformat()
    scan_value = _redact(scan, redact_paths=redact)
    safety = _safety_boundary()

    yield "{\n"
    yield f'  "schema_version": {_json_value(SCHEMA_VERSION)},\n'
    yield f'  "generated_at": {_json_value(generated_at)},\n'
    yield f'  "scan": {_indent_json(scan_value, 2)},\n'
    yield '  "resources": ['
    first = True
    for resource in store.iter_resources(scan_id):
        value = _redact(resource, redact_paths=redact)
        yield ("\n" if first else ",\n") + "    " + _indent_json(value, 4)
        first = False
    yield ("\n" if not first else "") + "  ],\n"
    yield '  "errors": ['
    first = True
    for error in store.iter_scan_errors(scan_id):
        value = _redact(error, redact_paths=redact)
        yield ("\n" if first else ",\n") + "    " + _indent_json(value, 4)
        first = False
    yield ("\n" if not first else "") + "  ],\n"
    yield '  "adapter_runs": ['
    first = True
    for adapter_run in store.iter_adapter_runs(scan_id):
        value = _redact(adapter_run, redact_paths=redact)
        yield ("\n" if first else ",\n") + "    " + _indent_json(value, 4)
        first = False
    yield ("\n" if not first else "") + "  ],\n"
    yield '  "evidence": ['
    first = True
    for evidence in store.iter_evidence(scan_id):
        value = _redact(evidence, redact_paths=redact)
        yield ("\n" if first else ",\n") + "    " + _indent_json(value, 4)
        first = False
    yield ("\n" if not first else "") + "  ],\n"
    yield f'  "safety_boundary": {_indent_json(safety, 2)}\n'
    yield "}\n"


def iter_markdown_report(store: StateStore, scan_id: str, *, redact: bool = True) -> Iterator[str]:
    """Yield a Markdown report using paged SQLite reads."""

    scan = store.get_scan(scan_id)
    if scan is None:
        raise KeyError(f"unknown scan: {scan_id}")
    store.validate_evidence_references(scan_id)
    scan_value = _redact(scan, redact_paths=redact)
    generated_at = utc_now().isoformat()
    yield "\n".join(
        (
            "# DevClean inventory report",
            "",
            f"- Scan: `{_cell(scan_value.get('scan_id'))}`",
            f"- Status: `{_cell(scan_value.get('status'))}`",
            f"- Generated: `{_cell(generated_at)}`",
            f"- Resources: {store.count_resources(scan_id)}",
            f"- Errors/boundaries: {store.count_scan_errors(scan_id)}",
            f"- Adapter runs: {store.count_adapter_runs(scan_id)}",
            f"- Evidence records: {store.count_evidence(scan_id)}",
            "",
            "> This export is report-only and cannot be imported or executed by DevClean.",
            "",
            "## Resources",
            "",
            "| Candidate | Adapter | Name | Locator | Type | Risk | Logical | Allocated | Path |",
            "|---|---|---|---|---|---:|---:|---:|---|",
            "",
        )
    )
    for item in store.iter_resources(scan_id):
        resource = _as_mapping(_redact(item, redact_paths=redact))
        logical = _as_mapping(resource.get("logical_size"))
        allocated = _as_mapping(resource.get("allocated_size"))
        yield (
            "| "
            + " | ".join(
                (
                    _cell(resource.get("candidate_id")),
                    _cell(resource.get("adapter_id")),
                    _cell(resource.get("display_name")),
                    _cell(resource.get("vendor_locator")),
                    _cell(resource.get("semantic_type")),
                    _cell(resource.get("risk_tier")),
                    _format_bytes(logical.get("value")),
                    _format_bytes(allocated.get("value")),
                    _cell(resource.get("path")),
                )
            )
            + " |\n"
        )

    if store.count_scan_errors(scan_id):
        yield "\n## Errors and boundaries\n\n"
        for item in store.iter_scan_errors(scan_id):
            error = _as_mapping(_redact(item, redact_paths=redact))
            yield (
                f"- `{_cell(error.get('kind'))}` {_cell(error.get('path'))}: "
                f"{_cell(error.get('message'))}\n"
            )
    if store.count_adapter_runs(scan_id):
        yield "\n## Adapter runs\n\n"
        for item in store.iter_adapter_runs(scan_id):
            run = _as_mapping(_redact(item, redact_paths=redact))
            yield (
                f"- `{_cell(run.get('adapter_id'))}` `{_cell(run.get('status'))}`; "
                f"effect `{_cell(run.get('effect_class'))}`; "
                f"version `{_cell(run.get('version'))}`\n"
            )
    if store.count_evidence(scan_id):
        yield "\n## Evidence metadata\n\n"
        for item in store.iter_evidence(scan_id):
            evidence = _as_mapping(_redact(item, redact_paths=redact))
            yield _evidence_markdown_line(evidence) + "\n"
    yield "\n"


def render_markdown(report: Mapping[str, Any]) -> str:
    scan = _as_mapping(report.get("scan"))
    resources = _as_sequence(report.get("resources"))
    errors = _as_sequence(report.get("errors"))
    adapter_runs = _as_sequence(report.get("adapter_runs"))
    evidence_records = _as_sequence(report.get("evidence"))
    lines = [
        "# DevClean inventory report",
        "",
        f"- Scan: `{_cell(scan.get('scan_id'))}`",
        f"- Status: `{_cell(scan.get('status'))}`",
        f"- Generated: `{_cell(report.get('generated_at'))}`",
        f"- Resources: {len(resources)}",
        f"- Errors/boundaries: {len(errors)}",
        f"- Adapter runs: {len(adapter_runs)}",
        f"- Evidence records: {len(evidence_records)}",
        "",
        "> This export is report-only and cannot be imported or executed by DevClean.",
        "",
        "## Resources",
        "",
        "| Candidate | Adapter | Name | Locator | Type | Risk | Logical | Allocated | Path |",
        "|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for item in resources:
        resource = _as_mapping(item)
        logical = _as_mapping(resource.get("logical_size"))
        allocated = _as_mapping(resource.get("allocated_size"))
        lines.append(
            "| "
            + " | ".join(
                (
                    _cell(resource.get("candidate_id")),
                    _cell(resource.get("adapter_id")),
                    _cell(resource.get("display_name")),
                    _cell(resource.get("vendor_locator")),
                    _cell(resource.get("semantic_type")),
                    _cell(resource.get("risk_tier")),
                    _format_bytes(logical.get("value")),
                    _format_bytes(allocated.get("value")),
                    _cell(resource.get("path")),
                )
            )
            + " |"
        )

    if errors:
        lines.extend(("", "## Errors and boundaries", ""))
        for item in errors:
            error = _as_mapping(item)
            lines.append(
                f"- `{_cell(error.get('kind'))}` {_cell(error.get('path'))}: "
                f"{_cell(error.get('message'))}"
            )
    if adapter_runs:
        lines.extend(("", "## Adapter runs", ""))
        for item in adapter_runs:
            run = _as_mapping(item)
            lines.append(
                f"- `{_cell(run.get('adapter_id'))}` `{_cell(run.get('status'))}`; "
                f"effect `{_cell(run.get('effect_class'))}`; "
                f"version `{_cell(run.get('version'))}`"
            )
    if evidence_records:
        lines.extend(("", "## Evidence metadata", ""))
        for item in evidence_records:
            evidence = _as_mapping(item)
            lines.append(_evidence_markdown_line(evidence))
    lines.append("")
    return "\n".join(lines)


def write_report_stream(path: Path, chunks: Iterable[str]) -> None:
    """Atomically publish a new local report without following or overwriting targets."""

    destination = Path(os.path.abspath(path))
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing report: {destination}")
    if not is_local_fixed_path(destination.parent):
        raise ValueError("report output must use a fixed local path without reparse ancestors")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not is_local_fixed_path(destination.parent):
        raise ValueError("report output parent changed across a reparse boundary")

    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.writelines(chunks)
            output.flush()
            os.fsync(output.fileno())
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"report target appeared during export: {destination}")
        if os.name == "nt":
            # MoveFile without REPLACE_EXISTING is atomic and refuses a racing target.
            os.rename(temporary, destination)
        else:
            # The portable test fallback publishes by exclusive hard-link creation.
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _redact(value: Any, *, redact_paths: bool) -> Any:
    if isinstance(value, str):
        return redact_text(value) if redact_paths else redact_secrets(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _safe_nonsecret_metadata(name, item):
                result[name] = item
            elif name in _INTERNAL_ID_LIST_FIELDS:
                if not isinstance(item, list) or any(
                    not isinstance(value, str) or not _SAFE_INTERNAL_ID.fullmatch(value)
                    for value in item
                ):
                    raise ValueError("stored internal ID list is invalid")
                result[name] = item
            elif redact_paths and name in {
                "path",
                "executable",
                "executable_path",
            } and isinstance(item, str):
                result[name] = redact_full_path(item)
            elif redact_paths and name == "roots" and isinstance(item, list):
                result[name] = [
                    redact_full_path(root) if isinstance(root, str) else root for root in item
                ]
            elif redact_paths and name == "vendor_locator" and item is not None:
                result[name] = "<VENDOR_LOCATOR>"
            else:
                result[name] = _redact(item, redact_paths=redact_paths)
        if redact_paths and result.get("vendor_locator") == "<VENDOR_LOCATOR>":
            adapter_id = str(result.get("adapter_id") or "vendor")
            result["display_name"] = f"{adapter_id} resource"
        return result
    if isinstance(value, list):
        return [_redact(item, redact_paths=redact_paths) for item in value]
    return value


def _safe_nonsecret_metadata(name: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if name in _INTERNAL_ID_FIELDS:
        return bool(_SAFE_INTERNAL_ID.fullmatch(value))
    if name.endswith("_sha256"):
        return bool(_SHA256.fullmatch(value))
    if name == "executable_file_id":
        return bool(_FILE_ID.fullmatch(value))
    if name == "executable_file_id_kind":
        return bool(_FILE_ID_KIND.fullmatch(value))
    if name == "executable_volume_serial":
        return bool(_VOLUME_SERIAL.fullmatch(value))
    if name in _EVIDENCE_FILE_FIELDS:
        return bool(_EVIDENCE_FILE.fullmatch(value))
    return False


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    visible: list[str] = []
    for character in str(value):
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character in {"\r", "\n"} or category in {"Zl", "Zp"}:
            visible.append(" ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F or category in {"Cf", "Cs"}:
            width = 4 if codepoint <= 0xFFFF else 8
            visible.append(f"\\u{codepoint:0{width}x}")
        else:
            visible.append(character)
    return escape("".join(visible), quote=False).replace("|", r"\|")


def _format_bytes(value: Any) -> str:
    if not isinstance(value, int):
        return "unknown"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0
    return f"{value} B"


def _evidence_markdown_line(evidence: Mapping[str, Any]) -> str:
    prefix = (
        f"- `{_cell(evidence.get('adapter_id'))}` "
        f"`{_cell(evidence.get('kind'))}`: "
    )
    if evidence.get("kind") == "LOOPBACK_API":
        outcome = _cell(evidence.get("outcome"))
        status = evidence.get("http_status")
        result = (
            f"HTTP {_cell(status)}"
            if status is not None
            else f"error `{_cell(evidence.get('error_type'))}`"
        )
        return (
            prefix
            + f"`GET {_cell(evidence.get('endpoint'))}` {outcome} {result}; "
            f"response source `{_cell(evidence.get('response_sha256'))}`, "
            f"stored `{_cell(evidence.get('response_stored_sha256'))}`"
        )
    return (
        prefix
        + f"stdout source `{_cell(evidence.get('stdout_sha256'))}`, "
        f"stored `{_cell(evidence.get('stdout_stored_sha256'))}`; "
        f"stderr source `{_cell(evidence.get('stderr_sha256'))}`, "
        f"stored `{_cell(evidence.get('stderr_stored_sha256'))}`"
    )


def _safety_boundary() -> dict[str, Any]:
    return {
        "executable": False,
        "statement": (
            "This report cannot be imported or executed. Arbitrary scan results are report-only."
        ),
    }


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _indent_json(value: Any, spaces: int) -> str:
    indentation = " " * spaces
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).replace(
        "\n", "\n" + indentation
    )
