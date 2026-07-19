from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from devclean.core.models import EffectClass
from devclean.core.reporting import render_markdown
from devclean.evidence import redaction
from devclean.evidence.models import LoopbackOutcome, TranscriptStorage
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)
from devclean.platform.windows.security import audit_private_directory

ROOT = Path(__file__).resolve().parents[1]


def _result(
    *, stdout: bytes, stderr: bytes, argv: tuple[str, ...] | None = None
) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv or (sys.executable, "--version"),
        returncode=0,
        stdout=stdout,
        stderr=stderr,
        duration_ms=7,
        termination=ProcessTermination.EXITED,
    )


def test_evidence_store_persists_only_redacted_transcripts_and_metadata(
    tmp_path: Path,
) -> None:
    stdout = (
        b'{"username":"alice","token":"source-token",'
        b'"endpoint":"https://private.example.test/org/repo?key=value",'
        b'"opaque":"AbCdEfGhIjKlMnOpQrStUvWx12345678"}\n'
    )
    stderr = b"Authorization: Bearer ey.secret.value\n/home/alice/private\n"
    result = _result(
        stdout=stdout,
        stderr=stderr,
        argv=(
            sys.executable,
            r"--cache-dir=C:\Users\Alice\private",
            "token=supersecret",
            "https://private.example.test/team",
        ),
    )
    store = EvidenceStore("scan_fixture", root=tmp_path)
    assert audit_private_directory(store.root).policy_satisfied

    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=result,
    )

    commands = tmp_path / "scan_fixture" / "commands"
    stored_stdout = (tmp_path / "scan_fixture" / evidence.stdout_file).read_bytes()
    stored_stderr = (tmp_path / "scan_fixture" / evidence.stderr_file).read_bytes()
    all_persisted = b"\n".join(path.read_bytes() for path in commands.iterdir())

    assert evidence.stdout_storage is TranscriptStorage.REDACTED_UTF8
    assert evidence.stderr_storage is TranscriptStorage.REDACTED_UTF8
    assert b"alice" not in all_persisted.lower()
    assert b"source-token" not in all_persisted
    assert b"supersecret" not in all_persisted
    assert b"private.example.test" not in all_persisted
    assert b"AbCdEfGhIjKlMnOpQrStUvWx12345678" not in all_persisted
    assert b"ey.secret.value" not in all_persisted
    assert b"<REDACTED" in stored_stdout
    assert b"<REDACTED" in stored_stderr

    assert evidence.stdout_size == len(stdout)
    assert evidence.stdout_sha256 == hashlib.sha256(stdout).hexdigest()
    assert evidence.stdout_stored_size == len(stored_stdout)
    assert evidence.stdout_stored_sha256 == hashlib.sha256(stored_stdout).hexdigest()
    assert evidence.stdout_sha256 != evidence.stdout_stored_sha256
    assert evidence.executable_path != sys.executable
    assert "<REDACTED_PATH>" in evidence.executable_path
    assert evidence.executable_sha256 == hashlib.sha256(
        Path(sys.executable).read_bytes()
    ).hexdigest()
    assert len(evidence.executable_sha256) == 64
    assert (
        evidence.executable_volume_serial,
        evidence.executable_file_id,
        evidence.executable_file_id_kind,
    ).count(None) in {0, 3}
    assert evidence.argv_redacted[0] != sys.executable
    assert evidence.argv_redacted[1] == "--cache-dir=<ABSOLUTE_PATH>"
    assert evidence.argv_redacted[2].endswith("<REDACTED>")
    assert evidence.argv_redacted[3] == "<REDACTED_URL>"

    metadata = json.loads((commands / f"{evidence.evidence_id}.meta.json").read_text("utf-8"))
    assert metadata["transcript_redaction_version"] == "transcript-redaction-v1"
    assert metadata["stdout_sha256"] == evidence.stdout_sha256
    assert metadata["stdout_stored_sha256"] == evidence.stdout_stored_sha256
    assert metadata["timed_out"] is False


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("executable_size", True),
        ("duration_ms", 1.5),
        ("stdout_size", -1),
        ("executable_path", "x" * 32_768),
        ("argv_redacted", ["not", "a", "tuple"]),
        ("stdout_sha256", None),
        ("captured_at", datetime.now()),
    ),
    ids=(
        "size-bool",
        "duration-float",
        "size-negative",
        "path-long",
        "argv-list",
        "hash-null",
        "time-naive",
    ),
)
def test_command_evidence_rejects_values_outside_report_schema(
    tmp_path: Path, field_name: str, value: object
) -> None:
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=_result(stdout=b"", stderr=b""),
    )

    with pytest.raises(ValueError):
        replace(evidence, **{field_name: value})


def test_evidence_store_uses_markers_for_non_utf8_and_unsafe_text(tmp_path: Path) -> None:
    stdout = b"\xff\xfesecret-binary"
    stderr = b"raw\x00token=secret"
    store = EvidenceStore("scan_fixture", root=tmp_path)

    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=_result(stdout=stdout, stderr=stderr),
    )

    stored_stdout = (tmp_path / "scan_fixture" / evidence.stdout_file).read_bytes()
    stored_stderr = (tmp_path / "scan_fixture" / evidence.stderr_file).read_bytes()
    assert evidence.stdout_storage is TranscriptStorage.NON_UTF8_MARKER
    assert evidence.stderr_storage is TranscriptStorage.UNSAFE_TEXT_MARKER
    assert stored_stdout.startswith(b"[DevClean_TRANSCRIPT_WITHHELD ")
    assert stored_stderr.startswith(b"[DevClean_TRANSCRIPT_WITHHELD ")
    assert stdout not in stored_stdout
    assert stderr not in stored_stderr
    assert evidence.stdout_sha256.encode("ascii") in stored_stdout
    assert evidence.stderr_sha256.encode("ascii") in stored_stderr
    assert evidence.stdout_stored_sha256 == hashlib.sha256(stored_stdout).hexdigest()
    assert evidence.stderr_stored_sha256 == hashlib.sha256(stored_stderr).hexdigest()


def test_command_evidence_rejects_executable_replacement_between_pre_and_post(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fixture.exe"
    shutil.copy2(sys.executable, executable)
    store = EvidenceStore("scan_fixture", root=tmp_path / "evidence")
    expected = store.observe_executable(executable)
    with executable.open("ab") as stream:
        stream.write(b"replacement")

    with pytest.raises(RuntimeError, match="changed during"):
        store.record_command(
            adapter_id="fixture",
            executable=executable,
            effect_class=EffectClass.PURE_QUERY,
            result=_result(stdout=b"", stderr=b"", argv=(str(executable),)),
            expected_executable=expected,
        )


def test_evidence_store_redaction_failure_never_falls_back_to_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"token=must-not-reach-disk"

    def fail_redaction(_: str) -> str:
        raise KeyError("simulated future redactor defect")

    monkeypatch.setattr(redaction, "_redact_transcript_text", fail_redaction)
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=_result(stdout=source, stderr=b""),
    )

    stored = (tmp_path / "scan_fixture" / evidence.stdout_file).read_bytes()
    assert evidence.stdout_storage is TranscriptStorage.REDACTION_ERROR_MARKER
    assert source not in stored
    assert stored.startswith(b"[DevClean_TRANSCRIPT_WITHHELD ")


def test_evidence_store_oversized_source_is_replaced_by_bounded_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redaction, "MAX_TRANSCRIPT_SOURCE_BYTES", 8)
    source = b"0123456789-secret"
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=_result(stdout=source, stderr=b""),
    )

    stored = (tmp_path / "scan_fixture" / evidence.stdout_file).read_bytes()
    assert evidence.stdout_storage is TranscriptStorage.SOURCE_TOO_LARGE_MARKER
    assert source not in stored
    assert len(stored) < 256


def test_command_evidence_matches_report_schema(tmp_path: Path) -> None:
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_command(
        adapter_id="fixture",
        executable=Path(sys.executable),
        effect_class=EffectClass.PURE_QUERY,
        result=_result(stdout=b"version 1.2.3\n", stderr=b""),
    )
    report_schema = json.loads(
        (ROOT / "schemas" / "scan-report.schema.json").read_text(encoding="utf-8")
    )
    evidence_schema = {
        "$schema": report_schema["$schema"],
        "$defs": report_schema["$defs"],
        "$ref": "#/$defs/evidence_record",
    }

    Draft202012Validator(evidence_schema).validate(evidence.to_dict())

    markdown = render_markdown(
        {
            "generated_at": evidence.captured_at.isoformat(),
            "scan": {"scan_id": evidence.scan_id, "status": "COMPLETED"},
            "resources": [],
            "errors": [],
            "evidence": [evidence.to_dict()],
        }
    )
    assert f"stdout source `{evidence.stdout_sha256}`" in markdown
    assert f"stored `{evidence.stdout_stored_sha256}`" in markdown


def test_loopback_evidence_persists_only_redacted_response_and_matches_schema(
    tmp_path: Path,
) -> None:
    source = (
        b'{"models":[{"name":"private/model","token":"must-not-reach-disk",'
        b'"endpoint":"https://private.example.test/team"}]}'
    )
    store = EvidenceStore("scan_fixture", root=tmp_path)

    evidence = store.record_loopback_response(
        adapter_id="ollama",
        endpoint="/api/tags",
        status=200,
        duration_ms=3,
        body=source,
        content_type="application/json; charset=utf-8",
        content_encoding=None,
    )

    stored = (tmp_path / "scan_fixture" / evidence.response_file).read_bytes()
    loopback = tmp_path / "scan_fixture" / "loopback"
    all_persisted = b"\n".join(path.read_bytes() for path in loopback.iterdir())
    assert source not in all_persisted
    assert b"must-not-reach-disk" not in all_persisted
    assert b"private.example.test" not in all_persisted
    assert b"<REDACTED" in stored
    assert evidence.outcome is LoopbackOutcome.RESPONSE
    assert evidence.response_size == len(source)
    assert evidence.response_sha256 == hashlib.sha256(source).hexdigest()
    assert evidence.response_stored_size == len(stored)
    assert evidence.response_stored_sha256 == hashlib.sha256(stored).hexdigest()
    assert evidence.response_sha256 != evidence.response_stored_sha256
    assert evidence.response_storage is TranscriptStorage.REDACTED_UTF8

    report_schema = json.loads(
        (ROOT / "schemas" / "scan-report.schema.json").read_text(encoding="utf-8")
    )
    evidence_schema = {
        "$schema": report_schema["$schema"],
        "$defs": report_schema["$defs"],
        "$ref": "#/$defs/evidence_record",
    }
    Draft202012Validator(evidence_schema).validate(evidence.to_dict())

    markdown = render_markdown(
        {
            "generated_at": evidence.captured_at.isoformat(),
            "scan": {"scan_id": evidence.scan_id, "status": "COMPLETED"},
            "resources": [],
            "errors": [],
            "evidence": [evidence.to_dict()],
        }
    )
    assert "`GET /api/tags` RESPONSE HTTP 200" in markdown
    assert f"response source `{evidence.response_sha256}`" in markdown

    with pytest.raises(ValueError, match="fixed Ollama loopback"):
        replace(evidence, host="localhost")
    with pytest.raises(ValueError, match="not allowlisted"):
        replace(evidence, endpoint="/redirect")
    with pytest.raises(ValueError, match="not allowlisted"):
        replace(evidence, method="POST")


def test_loopback_failure_has_a_redacted_empty_transcript_and_no_free_url(
    tmp_path: Path,
) -> None:
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_loopback_failure(
        adapter_id="ollama",
        endpoint="/api/version",
        error_type="TimeoutError",
        duration_ms=5000,
        timed_out=True,
        output_limit_exceeded=False,
    )

    assert evidence.outcome is LoopbackOutcome.FAILURE
    assert evidence.http_status is None
    assert evidence.error_type == "TimeoutError"
    assert evidence.timed_out is True
    assert evidence.host == "127.0.0.1"
    assert evidence.port == 11434
    assert evidence.method == "GET"
    assert evidence.response_size == 0
    assert (tmp_path / "scan_fixture" / evidence.response_file).read_bytes() == b""
    assert "url" not in evidence.to_dict()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("duration_ms", True),
        ("response_size", 1.5),
        ("response_stored_size", -1),
        ("response_sha256", None),
        ("captured_at", datetime.now()),
    ),
    ids=("duration-bool", "size-float", "stored-size-negative", "hash-null", "time-naive"),
)
def test_loopback_evidence_rejects_values_outside_report_schema(
    tmp_path: Path, field_name: str, value: object
) -> None:
    store = EvidenceStore("scan_fixture", root=tmp_path)
    evidence = store.record_loopback_failure(
        adapter_id="ollama",
        endpoint="/api/version",
        error_type="ConnectionError",
        duration_ms=1,
        timed_out=False,
        output_limit_exceeded=False,
    )

    with pytest.raises(ValueError):
        replace(evidence, **{field_name: value})


def test_loopback_store_enforces_the_endpoint_bound_before_redaction(tmp_path: Path) -> None:
    store = EvidenceStore("scan_fixture", root=tmp_path)

    with pytest.raises(ValueError, match="bounded endpoint limit"):
        store.record_loopback_response(
            adapter_id="ollama",
            endpoint="/api/version",
            status=200,
            duration_ms=1,
            body=b"x" * (64 * 1024 + 1),
            content_type="application/json",
            content_encoding=None,
        )

    assert not (tmp_path / "scan_fixture" / "loopback").exists()


def test_evidence_store_rejects_path_shaped_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scan_id"):
        EvidenceStore("../escape", root=tmp_path)
    store = EvidenceStore("scan_fixture", root=tmp_path)
    with pytest.raises(ValueError, match="adapter_id"):
        store.record_command(
            adapter_id="../escape",
            executable=Path(sys.executable),
            effect_class=EffectClass.PURE_QUERY,
            result=_result(stdout=b"", stderr=b""),
        )
