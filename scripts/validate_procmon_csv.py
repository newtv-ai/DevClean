"""Conservatively validate a filtered Process Monitor CSV for G2 evidence.

This utility never changes managed data.  It streams an English-header ProcMon
CSV, rejects unclassified operations, rejects every registry mutation and every
file mutation outside explicitly named operational-write roots, and reports the
small set of writes that were allowlisted.  It is only one part of G2: the
capture configuration, PML file, service-state snapshots and multi-machine
manifest remain required by the acceptance protocol.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import ntpath
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

SCHEMA_VERSION = "1.0.0"
REQUIRED_COLUMNS = ("Process Name", "PID", "Operation", "Path", "Result", "Detail")
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,126}[A-Za-z0-9)]$")
MAX_CSV_BYTES = 16 * 1024 * 1024 * 1024
MAX_FINDINGS = 10_000
MAX_REQUIRED_PROCESSES = 64
MAX_ROWS = 10_000_000

_FILE_WRITE_ACCESS = (
    "generic write",
    "generic read/write",
    "write data",
    "add file",
    "append data",
    "add subdirectory",
    "write ea",
    "write attributes",
    "delete",
    "delete child",
    "write dac",
    "write owner",
    "maximum allowed",
    "all access",
)
_MUTATING_DISPOSITIONS = ("create", "openif", "overwrite", "overwriteif", "supersede")
_REGISTRY_MUTATIONS = {
    "regdeletekey",
    "regdeletevalue",
    "regloadkey",
    "regrenamekey",
    "regreplacekey",
    "regrestorekey",
    "regsavekey",
    "regsetinformationkey",
    "regsetkeysecurity",
    "regsetvalue",
    "regunloadkey",
}
_NONMUTATING_EXACT = {
    "closefile",
    "cleanup",
    "directorycontrol",
    "flushbuffersfile",
    "lockfile",
    "unlockfile",
    "process exit",
    "process profiling",
    "thread create",
    "thread exit",
    "load image",
    "regclosekey",
    "regenumkey",
    "regenumvalue",
    "regopenkey",
    "regquerykey",
    "regquerykeysecurity",
    "regquerymultiplevaluekey",
    "regqueryvalue",
}


@dataclass(frozen=True)
class RootRule:
    label: str
    canonical_path: str


@dataclass(frozen=True)
class Finding:
    line: int
    process_name: str
    operation: str
    result: str
    path_sha256: str
    code: str
    matched_root: str | None


@dataclass(frozen=True)
class Classification:
    kind: str
    code: str


def parse_root_rule(value: str) -> RootRule:
    """Parse ``label=C:\\absolute\\path`` without exposing the path in output."""

    label, separator, raw_path = value.partition("=")
    if not separator or not LABEL_RE.fullmatch(label):
        raise ValueError("root must use a lowercase opaque label: label=C:\\absolute\\path")
    canonical = canonical_windows_path(raw_path)
    drive, tail = ntpath.splitdrive(canonical)
    if not re.fullmatch(r"[a-z]:", drive) or not tail.startswith("\\"):
        raise ValueError(f"root {label!r} must be an absolute drive-letter Windows path")
    if tail == "\\":
        raise ValueError(f"root {label!r} cannot be an entire volume")
    return RootRule(label=label, canonical_path=canonical.rstrip("\\"))


def canonical_windows_path(value: str) -> str:
    if not value or "\x00" in value or "%" in value or "<" in value or ">" in value:
        raise ValueError("path is empty, templated, or contains a forbidden character")
    normalized = ntpath.normpath(value.replace("/", "\\"))
    return normalized.casefold()


def _is_under(path: str, root: RootRule) -> bool:
    return path == root.canonical_path or path.startswith(root.canonical_path + "\\")


def _matching_root(path: str, roots: Sequence[RootRule]) -> RootRule | None:
    matches = [root for root in roots if _is_under(path, root)]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item.canonical_path))


def validate_root_sets(
    protected_roots: Sequence[RootRule], allowed_write_roots: Sequence[RootRule]
) -> None:
    labels = [root.label for root in (*protected_roots, *allowed_write_roots)]
    if len(labels) != len(set(labels)):
        raise ValueError("root labels must be unique across protected and allowed sets")
    for protected in protected_roots:
        for allowed in allowed_write_roots:
            if _is_under(protected.canonical_path, allowed) or _is_under(
                allowed.canonical_path, protected
            ):
                raise ValueError(
                    "protected and allowed-write roots must not overlap: "
                    f"{protected.label!r}, {allowed.label!r}"
                )


def _path_digest(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()


def _detail_field(detail: str, name: str) -> str | None:
    # ProcMon values such as Desired Access are themselves comma-separated.
    # Stop only at a subsequent ``Field Name:`` token, not at the first comma.
    match = re.search(
        rf"(?:^|,\s*){re.escape(name)}:\s*(.*?)"
        rf"(?=,\s*[A-Za-z][A-Za-z0-9 ()/_-]{{0,63}}:\s*|$)",
        detail,
        re.IGNORECASE,
    )
    return match.group(1).strip().casefold() if match else None


def _is_named_pipe(path: str) -> bool:
    lowered = path.casefold().replace("/", "\\")
    return lowered.startswith("\\device\\namedpipe\\") or lowered.startswith(
        "\\\\.\\pipe\\"
    )


def _classify_create_file(path: str, detail: str) -> Classification:
    if _is_named_pipe(path):
        return Classification("NONMUTATING", "NAMED_PIPE_IPC")
    desired_access = _detail_field(detail, "Desired Access")
    disposition = _detail_field(detail, "Disposition")
    if disposition in _MUTATING_DISPOSITIONS:
        return Classification("FILE_MUTATION", "MUTATING_CREATEFILE_DISPOSITION")
    if desired_access is None:
        return Classification("UNKNOWN", "CREATEFILE_ACCESS_MISSING")
    if any(token in desired_access for token in _FILE_WRITE_ACCESS):
        return Classification("FILE_MUTATION", "MUTATING_CREATEFILE_ACCESS")
    return Classification("NONMUTATING", "READ_ONLY_CREATEFILE")


def _classify_registry_create(detail: str) -> Classification:
    disposition = _detail_field(detail, "Disposition")
    if disposition == "reg_opened_existing_key":
        return Classification("NONMUTATING", "REGISTRY_KEY_OPENED")
    if disposition == "reg_created_new_key":
        return Classification("REGISTRY_MUTATION", "REGISTRY_KEY_CREATED")
    return Classification("UNKNOWN", "REGCREATEKEY_DISPOSITION_MISSING")


def classify_operation(operation: str, path: str, detail: str) -> Classification:
    normalized = operation.strip().casefold()
    if normalized in {"process create", "process start"}:
        return Classification("PROCESS_EVENT", "PROCESS_STARTED")
    if normalized == "createfile":
        return _classify_create_file(path, detail)
    if normalized == "createfilemapping":
        protection = _detail_field(detail, "Protection")
        if protection in {"page_readonly", "page_execute_read"}:
            return Classification("NONMUTATING", "READ_ONLY_FILE_MAPPING")
        if protection is None:
            return Classification("UNKNOWN", "FILE_MAPPING_PROTECTION_MISSING")
        return Classification("FILE_MUTATION", "WRITABLE_FILE_MAPPING")
    if normalized == "regcreatekey":
        return _classify_registry_create(detail)
    if normalized in _REGISTRY_MUTATIONS:
        return Classification("REGISTRY_MUTATION", "REGISTRY_MUTATION")
    if normalized in _NONMUTATING_EXACT:
        return Classification("NONMUTATING", "KNOWN_NONMUTATING")
    if normalized.startswith(("query", "read", "enumerate")):
        return Classification("NONMUTATING", "KNOWN_READ_OPERATION")
    if normalized.startswith(("tcp ", "udp ")):
        return Classification("NETWORK_EVENT", "NETWORK_OBSERVATION")
    if normalized.startswith(("write", "set", "delete", "rename")):
        return Classification("FILE_MUTATION", "FILE_MUTATION")
    return Classification("UNKNOWN", "UNCLASSIFIED_OPERATION")


def _forbidden_process_reason(process_name: str, path: str, detail: str) -> str | None:
    image = ntpath.basename(path.replace("/", "\\")).casefold()
    process = process_name.casefold()
    text = f"{process_name} {path} {detail}".casefold()
    tokenized = re.sub(r"[^a-z0-9_.+-]+", " ", text)
    if image == "dism.exe" or process == "dism.exe":
        return "DISM_LAUNCHED"
    if re.search(r"\bnpm(?:-cli)?(?:\.js|\.cmd|\.exe)?\b.*\bcache\s+verify\b", tokenized):
        return "NPM_CACHE_VERIFY_LAUNCHED"
    if image == "docker desktop.exe" or process == "docker desktop.exe":
        return "DOCKER_DESKTOP_LAUNCHED"
    if re.search(r"\bdocker(?:\.exe)?\s+desktop\b", text):
        return "DOCKER_DESKTOP_LAUNCHED"
    if image == "ollama app.exe" or process == "ollama app.exe":
        return "OLLAMA_APP_LAUNCHED"
    if re.search(r"\bollama(?:\.exe)?\s+serve\b", text):
        return "OLLAMA_SERVICE_LAUNCHED"
    if (image in {"sc.exe", "net.exe"} or process in {"sc.exe", "net.exe"}) and re.search(
        r"\sstart\s", f" {text} "
    ):
        return "SERVICE_START_COMMAND_LAUNCHED"
    if (image in {"powershell.exe", "pwsh.exe"} or process in {"powershell.exe", "pwsh.exe"}) and (
        "start-service" in text
    ):
        return "SERVICE_START_COMMAND_LAUNCHED"
    return None


def _network_violation(path: str) -> str | None:
    _local, separator, remote = path.casefold().partition(" -> ")
    if not separator:
        return "NETWORK_ENDPOINT_UNPARSEABLE"
    endpoint = remote.strip()
    if endpoint.startswith(("127.0.0.1:", "localhost:", "[::1]:")):
        return None
    return "NON_LOOPBACK_NETWORK"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file_state(before: os.stat_result, after: os.stat_result) -> bool:
    return bool(
        before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and (not before.st_dev or not after.st_dev or before.st_dev == after.st_dev)
        and (not before.st_ino or not after.st_ino or before.st_ino == after.st_ino)
    )


def _header_mapping(fieldnames: Sequence[str | None] | None) -> dict[str, str]:
    if not fieldnames:
        raise ValueError("CSV has no header")
    if any(name is None for name in fieldnames):
        raise ValueError("CSV contains an invalid header")
    lowered = [str(name).strip().casefold() for name in fieldnames]
    if len(lowered) != len(set(lowered)):
        raise ValueError("CSV contains duplicate headers")
    mapping = {str(name).strip().casefold(): str(name) for name in fieldnames}
    missing = [name for name in REQUIRED_COLUMNS if name.casefold() not in mapping]
    if missing:
        raise ValueError(f"CSV is missing required English headers: {', '.join(missing)}")
    return mapping


def _safe_field(row: dict[str | None, str | list[str] | None], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise ValueError(f"CSV row is malformed at field {key!r}")
    if "\x00" in value:
        raise ValueError(f"CSV row contains NUL at field {key!r}")
    return value.strip()


def _safe_pid(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("CSV PID must be an unsigned decimal integer")
    pid = int(value)
    if pid < 0 or pid > 0xFFFFFFFF:
        raise ValueError("CSV PID is outside the Windows process-ID range")
    return pid


def _validate_required_processes(values: Sequence[str]) -> set[str]:
    if not values or len(values) > MAX_REQUIRED_PROCESSES:
        raise ValueError("required process count is empty or exceeds its bound")
    normalized: list[str] = []
    for value in values:
        process = value.strip()
        if (
            not PROCESS_NAME_RE.fullmatch(process)
            or "\\" in process
            or "/" in process
            or "\x00" in process
        ):
            raise ValueError("required process names must be bounded basenames")
        normalized.append(process.casefold())
    if len(set(normalized)) != len(normalized):
        raise ValueError("required process names must be unique case-insensitively")
    return set(normalized)


def audit_procmon_csv(
    csv_path: Path,
    *,
    protected_roots: Sequence[RootRule],
    allowed_write_roots: Sequence[RootRule],
    required_processes: Sequence[str],
    max_findings: int = 200,
    max_rows: int = MAX_ROWS,
    expected_csv_sha256: str | None = None,
) -> dict[str, object]:
    if not protected_roots or not allowed_write_roots:
        raise ValueError("at least one protected root and one allowed-write root are required")
    if max_findings < 1 or max_findings > MAX_FINDINGS:
        raise ValueError("max_findings is outside its accepted boundary")
    if max_rows < 1 or max_rows > MAX_ROWS:
        raise ValueError("max_rows is outside its accepted boundary")
    validate_root_sets(protected_roots, allowed_write_roots)
    expected_processes = _validate_required_processes(required_processes)

    before_state = csv_path.stat(follow_symlinks=False)
    if csv_path.is_symlink() or int(
        getattr(before_state, "st_file_attributes", 0)
    ) & 0x00000400:
        raise ValueError("CSV must be an ordinary non-reparse file")
    csv_size = before_state.st_size
    if csv_size < 1 or csv_size > MAX_CSV_BYTES:
        raise ValueError("CSV size is outside the accepted boundary")
    csv_sha256 = _file_sha256(csv_path)
    if expected_csv_sha256 is not None:
        if not HEX_64_RE.fullmatch(expected_csv_sha256):
            raise ValueError("expected CSV SHA-256 must be 64 lowercase hexadecimal characters")
        if csv_sha256 != expected_csv_sha256:
            raise ValueError("CSV SHA-256 does not match --expected-csv-sha256")

    operation_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    violation_code_counts: Counter[str] = Counter()
    allowed_write_summary: Counter[tuple[str, str, str]] = Counter()
    observed_processes: set[str] = set()
    findings: list[Finding] = []
    allowed_writes: list[Finding] = []
    violation_count = 0
    allowed_write_count = 0
    network_event_count = 0
    loopback_network_event_count = 0
    row_count = 0

    csv.field_size_limit(1024 * 1024)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        mapping = _header_mapping(reader.fieldnames)
        columns = {name: mapping[name.casefold()] for name in REQUIRED_COLUMNS}
        for row in reader:
            row_count += 1
            if row_count > max_rows:
                raise ValueError("CSV row count exceeds --max-rows")
            if None in row:
                raise ValueError(f"CSV row {reader.line_num} has more fields than its header")
            process_name = _safe_field(row, columns["Process Name"])
            if not process_name or len(process_name) > 128:
                raise ValueError("CSV process name is empty or exceeds its bound")
            _safe_pid(_safe_field(row, columns["PID"]))
            operation = _safe_field(row, columns["Operation"])
            if not operation or len(operation) > 128:
                raise ValueError("CSV operation is empty or exceeds its bound")
            raw_path = _safe_field(row, columns["Path"])
            if len(raw_path) > 32767:
                raise ValueError("CSV path exceeds the Windows path boundary")
            result = _safe_field(row, columns["Result"])
            if not result or len(result) > 256:
                raise ValueError("CSV result is empty or exceeds its bound")
            detail = _safe_field(row, columns["Detail"])
            if len(detail) > 1024 * 1024:
                raise ValueError("CSV detail exceeds its field boundary")
            observed_processes.add(process_name.casefold())
            operation_counts[operation] += 1
            classification = classify_operation(operation, raw_path, detail)
            classification_counts[classification.kind] += 1
            path_sha256 = _path_digest(raw_path)

            code: str | None = None
            matched_root: str | None = None
            if classification.kind == "PROCESS_EVENT":
                code = _forbidden_process_reason(process_name, raw_path, detail)
            elif classification.kind == "NETWORK_EVENT":
                network_event_count += 1
                code = _network_violation(raw_path)
                if code is None:
                    loopback_network_event_count += 1
            elif classification.kind in {"REGISTRY_MUTATION", "UNKNOWN"}:
                code = classification.code
            elif classification.kind == "FILE_MUTATION":
                try:
                    canonical = canonical_windows_path(raw_path)
                except ValueError:
                    code = "UNRESOLVED_WRITE_PATH"
                else:
                    protected = _matching_root(canonical, protected_roots)
                    allowed = _matching_root(canonical, allowed_write_roots)
                    if protected is not None:
                        code = "PROTECTED_ROOT_WRITE"
                        matched_root = protected.label
                    elif allowed is not None:
                        allowed_write_count += 1
                        matched_root = allowed.label
                        allowed_write_summary[(allowed.label, operation, result)] += 1
                        if len(allowed_writes) < max_findings:
                            allowed_writes.append(
                                Finding(
                                    line=reader.line_num,
                                    process_name=process_name,
                                    operation=operation,
                                    result=result,
                                    path_sha256=path_sha256,
                                    code=classification.code,
                                    matched_root=matched_root,
                                )
                            )
                    else:
                        code = "WRITE_OUTSIDE_ALLOWLIST"

            if code is not None:
                violation_count += 1
                violation_code_counts[code] += 1
                if len(findings) < max_findings:
                    findings.append(
                        Finding(
                            line=reader.line_num,
                            process_name=process_name,
                            operation=operation,
                            result=result,
                            path_sha256=path_sha256,
                            code=code,
                            matched_root=matched_root,
                        )
                    )

    missing_processes = sorted(expected_processes - observed_processes)
    for process in missing_processes:
        violation_count += 1
        violation_code_counts["REQUIRED_PROCESS_NOT_OBSERVED"] += 1
        if len(findings) < max_findings:
            findings.append(
                Finding(
                    line=1,
                    process_name=process,
                    operation="<capture>",
                    result="",
                    path_sha256=_path_digest(""),
                    code="REQUIRED_PROCESS_NOT_OBSERVED",
                    matched_root=None,
                )
            )
    if row_count == 0:
        violation_count += 1
        violation_code_counts["EMPTY_CAPTURE"] += 1
        findings.append(
            Finding(
                line=1,
                process_name="",
                operation="<capture>",
                result="",
                path_sha256=_path_digest(""),
                code="EMPTY_CAPTURE",
                matched_root=None,
            )
        )

    after_parse_state = csv_path.stat(follow_symlinks=False)
    after_parse_sha256 = _file_sha256(csv_path)
    final_state = csv_path.stat(follow_symlinks=False)
    if (
        not _same_file_state(before_state, after_parse_state)
        or not _same_file_state(before_state, final_state)
        or after_parse_sha256 != csv_sha256
    ):
        raise RuntimeError("CSV changed during validation")

    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": datetime.now(UTC).isoformat(),
        "validator_sha256": _file_sha256(Path(__file__)),
        "verdict": "PASS" if violation_count == 0 else "FAIL",
        "csv_sha256": csv_sha256,
        "csv_bytes": csv_size,
        "rows": row_count,
        "max_rows": max_rows,
        "required_processes": sorted(expected_processes),
        "observed_required_processes": sorted(expected_processes & observed_processes),
        "protected_root_labels": sorted(root.label for root in protected_roots),
        "allowed_write_root_labels": sorted(root.label for root in allowed_write_roots),
        "operation_counts": dict(sorted(operation_counts.items())),
        "classification_counts": dict(sorted(classification_counts.items())),
        "network_event_count": network_event_count,
        "loopback_network_event_count": loopback_network_event_count,
        "non_loopback_network_count": network_event_count - loopback_network_event_count,
        "allowed_write_count": allowed_write_count,
        "allowed_write_summary": [
            {
                "matched_root": root,
                "operation": operation,
                "result": result,
                "count": count,
            }
            for (root, operation, result), count in sorted(allowed_write_summary.items())
        ],
        "allowed_writes": [asdict(item) for item in allowed_writes],
        "allowed_writes_truncated": allowed_write_count > len(allowed_writes),
        "violation_count": violation_count,
        "violation_code_counts": dict(sorted(violation_code_counts.items())),
        "violations": [asdict(item) for item in findings],
        "violations_truncated": violation_count > len(findings),
        "limitations": [
            "This result does not prove that ProcMon filters captured the complete process tree.",
            (
                "Retain the PML file, filter screenshot/configuration, service-state "
                "snapshots, and gate manifest."
            ),
            (
                "Path values are represented only by SHA-256 in this result; the source "
                "CSV remains sensitive local evidence."
            ),
        ],
    }


def _parse_roots(values: Iterable[str]) -> tuple[RootRule, ...]:
    return tuple(parse_root_rule(value) for value in values)


def _write_json(payload: dict[str, object], output: Path | None, stream: IO[str]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        stream.write(rendered)
    else:
        if output.exists():
            raise FileExistsError(f"refusing to overwrite existing evidence: {output}")
        if not output.parent.is_dir():
            raise FileNotFoundError(f"evidence output parent does not exist: {output.parent}")
        output.write_text(rendered, encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="English-header ProcMon CSV export")
    parser.add_argument(
        "--protected-root",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="managed cache or user-asset root that must never receive writes",
    )
    parser.add_argument(
        "--allowed-write-root",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="declared devclean/vendor operational-write sandbox",
    )
    parser.add_argument(
        "--required-process",
        action="append",
        required=True,
        help="process name that must occur in the filtered trace",
    )
    parser.add_argument("--expected-csv-sha256")
    parser.add_argument("--max-findings", type=int, default=200)
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    parser.add_argument("--output", type=Path, help="write redacted JSON result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = audit_procmon_csv(
            args.csv,
            protected_roots=_parse_roots(args.protected_root),
            allowed_write_roots=_parse_roots(args.allowed_write_root),
            required_processes=args.required_process,
            max_findings=args.max_findings,
            max_rows=args.max_rows,
            expected_csv_sha256=args.expected_csv_sha256,
        )
        _write_json(result, args.output, sys.stdout)
    except (OSError, UnicodeError, csv.Error, RuntimeError, ValueError) as exc:
        print(f"ProcMon validation error: {exc}", file=sys.stderr)
        return 2
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
