from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_procmon_csv",
    ROOT / "scripts" / "validate_procmon_csv.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load ProcMon validator")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

parse_root_rule = cast(Callable[[str], Any], MODULE.parse_root_rule)
classify_operation = cast(Callable[[str, str, str], Any], MODULE.classify_operation)
audit_procmon_csv = cast(Callable[..., dict[str, object]], MODULE.audit_procmon_csv)
validate_root_sets = cast(Callable[[Sequence[Any], Sequence[Any]], None], MODULE.validate_root_sets)
write_json = cast(Callable[[dict[str, object], Path | None, io.StringIO], None], MODULE._write_json)

HEADERS = ("Process Name", "PID", "Operation", "Path", "Result", "Detail")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _row(
    operation: str,
    path: str,
    detail: str = "",
    *,
    process: str = "python.exe",
    pid: str = "4242",
    result: str = "SUCCESS",
) -> dict[str, str]:
    return {
        "Process Name": process,
        "PID": pid,
        "Operation": operation,
        "Path": path,
        "Result": result,
        "Detail": detail,
    }


def _rules() -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    protected = (
        parse_root_rule(r"managed_cache=C:\fixture\managed"),
        parse_root_rule(r"user_assets=C:\fixture\protected"),
    )
    allowed = (parse_root_rule(r"devclean_data=C:\fixture\state"),)
    return protected, allowed


def _audit(path: Path) -> dict[str, object]:
    protected, allowed = _rules()
    return audit_procmon_csv(
        path,
        protected_roots=protected,
        allowed_write_roots=allowed,
        required_processes=("python.exe",),
    )


def test_createfile_detail_parser_does_not_lose_write_right_after_comma() -> None:
    classification = classify_operation(
        "CreateFile",
        r"C:\fixture\managed\item.bin",
        "Desired Access: Read Attributes, Write Data, Synchronize, Disposition: Open",
    )

    assert classification.kind == "FILE_MUTATION"
    assert classification.code == "MUTATING_CREATEFILE_ACCESS"


def test_read_only_createfile_with_multiple_access_tokens_stays_observational() -> None:
    classification = classify_operation(
        "CreateFile",
        r"C:\fixture\managed\item.bin",
        "Desired Access: Read Data/List Directory, Read Attributes, Synchronize, "
        "Disposition: Open, Options: Non-Directory File",
    )

    assert classification.kind == "NONMUTATING"
    assert classification.code == "READ_ONLY_CREATEFILE"


def test_root_rules_are_segment_bounded_and_cannot_overlap() -> None:
    protected = (parse_root_rule(r"protected=C:\fixture\root"),)
    allowed = (parse_root_rule(r"allowed=C:\fixture\root\child"),)

    with pytest.raises(ValueError, match="overlap"):
        validate_root_sets(protected, allowed)
    with pytest.raises(ValueError, match="entire volume"):
        parse_root_rule("drive=C:\\")
    with pytest.raises(ValueError, match="drive-letter"):
        parse_root_rule(r"unc=\\server\share")


def test_procmon_pass_allows_only_declared_write_and_loopback_network(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace.csv"
    _write_csv(
        csv_path,
        [
            _row(
                "Process Create",
                r"C:\Python\python.exe",
                "Command line: python.exe -m DevClean scan",
            ),
            _row(
                "CreateFile",
                r"C:\fixture\managed\cache.bin",
                "Desired Access: Read Data/List Directory, Synchronize, Disposition: Open",
            ),
            _row("WriteFile", r"C:\fixture\state\DevClean.db"),
            _row("TCP Send", "10.0.0.2:52000 -> 127.0.0.1:11434"),
        ],
    )

    result = _audit(csv_path)

    assert result["verdict"] == "PASS"
    assert result["violation_count"] == 0
    assert result["allowed_write_count"] == 1
    assert result["network_event_count"] == 1
    assert result["loopback_network_event_count"] == 1
    assert result["non_loopback_network_count"] == 0
    assert result["allowed_write_summary"] == [
        {
            "matched_root": "devclean_data",
            "operation": "WriteFile",
            "result": "SUCCESS",
            "count": 1,
        }
    ]
    assert result["csv_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert isinstance(result["validator_sha256"], str)


@pytest.mark.parametrize(
    ("row", "code"),
    (
        (_row("WriteFile", r"C:\fixture\managed\cache.bin"), "PROTECTED_ROOT_WRITE"),
        (_row("WriteFile", r"C:\outside\file.bin"), "WRITE_OUTSIDE_ALLOWLIST"),
        (_row("MysteryOperation", r"C:\fixture\managed"), "UNCLASSIFIED_OPERATION"),
        (
            _row("TCP Send", "10.0.0.2:52000 -> 203.0.113.9:443"),
            "NON_LOOPBACK_NETWORK",
        ),
        (
            _row(
                "Process Create",
                r"C:\Program Files\nodejs\npm.cmd",
                'Command line: "npm.cmd" "cache" "verify"',
            ),
            "NPM_CACHE_VERIFY_LAUNCHED",
        ),
    ),
)
def test_procmon_closed_set_rejects_unsafe_events(
    tmp_path: Path, row: dict[str, str], code: str
) -> None:
    csv_path = tmp_path / "trace.csv"
    _write_csv(csv_path, [row])

    result = _audit(csv_path)

    assert result["verdict"] == "FAIL"
    assert result["violation_count"] == 1
    assert result["violation_code_counts"] == {code: 1}


def test_procmon_requires_every_declared_process(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace.csv"
    _write_csv(csv_path, [_row("ReadFile", r"C:\fixture\managed\cache.bin")])
    protected, allowed = _rules()

    result = audit_procmon_csv(
        csv_path,
        protected_roots=protected,
        allowed_write_roots=allowed,
        required_processes=("python.exe", "docker.exe"),
    )

    assert result["verdict"] == "FAIL"
    assert result["violation_code_counts"] == {"REQUIRED_PROCESS_NOT_OBSERVED": 1}


def test_procmon_rejects_malformed_pid_and_hash_mismatch(tmp_path: Path) -> None:
    csv_path = tmp_path / "trace.csv"
    _write_csv(
        csv_path,
        [_row("ReadFile", r"C:\fixture\managed\cache.bin", pid="4e2")],
    )
    with pytest.raises(ValueError, match="PID"):
        _audit(csv_path)

    _write_csv(csv_path, [_row("ReadFile", r"C:\fixture\managed\cache.bin")])
    protected, allowed = _rules()
    with pytest.raises(ValueError, match="does not match"):
        audit_procmon_csv(
            csv_path,
            protected_roots=protected,
            allowed_write_roots=allowed,
            required_processes=("python.exe",),
            expected_csv_sha256="0" * 64,
        )


def test_procmon_output_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    output = tmp_path / "result.json"
    output.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="refusing"):
        write_json({"verdict": "PASS"}, output, io.StringIO())


def test_procmon_module_loaded_as_expected() -> None:
    assert isinstance(MODULE, ModuleType)
