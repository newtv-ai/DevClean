"""Crash-tolerant local storage for bounded command transcripts."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from devclean.core.models import EffectClass, new_id, utc_now
from devclean.core.paths import data_dir
from devclean.evidence.models import (
    LOOPBACK_HOST,
    LOOPBACK_METHOD,
    LOOPBACK_PORT,
    LOOPBACK_RESPONSE_LIMITS,
    CommandEvidence,
    EvidenceKind,
    LoopbackContentEncodingClass,
    LoopbackContentTypeClass,
    LoopbackEvidence,
    LoopbackOutcome,
)
from devclean.evidence.redaction import (
    TRANSCRIPT_REDACTION_VERSION,
    redact_argument,
    redact_full_path,
    redact_transcript_bytes,
)
from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.process import BoundedProcessResult
from devclean.platform.windows.security import secure_private_directory
from devclean.platform.windows.volumes import is_local_fixed_path

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_EXECUTABLE_HASH_BYTES = 1024 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ExecutableObservation:
    size: int
    mtime_ns: int
    volume_serial: str | None
    file_id: str | None
    file_id_kind: str | None
    sha256: str


class EvidenceStore:
    """Write local-only evidence under a scan-scoped application directory."""

    def __init__(self, scan_id: str, root: Path | None = None) -> None:
        if not _SAFE_ID.fullmatch(scan_id):
            raise ValueError("scan_id is not safe for an evidence path")
        self.scan_id = scan_id
        self.root = (root or (data_dir() / "evidence")) / scan_id
        if not self.root.is_absolute() or str(self.root).startswith((r"\\", "//")):
            raise ValueError("evidence root must be an absolute local path")
        if not is_local_fixed_path(self.root):
            raise ValueError(
                "evidence root must use a fixed local volume without reparse ancestors"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        secure_private_directory(self.root)
        self._executable_cache: dict[tuple[object, ...], ExecutableObservation] = {}

    def observe_executable(self, executable: Path) -> ExecutableObservation:
        """Capture the executable identity before a vendor query is launched."""

        return self._observe_executable(executable, force_hash=False)

    def record_command(
        self,
        *,
        adapter_id: str,
        executable: Path,
        effect_class: EffectClass,
        result: BoundedProcessResult,
        expected_executable: ExecutableObservation | None = None,
    ) -> CommandEvidence:
        """Persist redacted transcripts first and publish metadata last.

        ``result`` remains available to the adapter parser in memory; source
        bytes are hashed here but are never passed to the filesystem writer.
        """

        if not _SAFE_ID.fullmatch(adapter_id):
            raise ValueError("adapter_id is not safe for an evidence path")
        executable_observation = self._observe_executable(executable, force_hash=True)
        if (
            expected_executable is not None
            and executable_observation != expected_executable
        ):
            raise RuntimeError("query executable changed during command observation")
        evidence_id = new_id("evidence")
        commands = self.root / "commands"
        commands.mkdir(parents=True, exist_ok=True)
        stdout_name = f"{evidence_id}.stdout.redacted.txt"
        stderr_name = f"{evidence_id}.stderr.redacted.txt"
        meta_name = f"{evidence_id}.meta.json"
        stdout = redact_transcript_bytes(result.stdout)
        stderr = redact_transcript_bytes(result.stderr)
        _atomic_write(commands / stdout_name, stdout.content)
        _atomic_write(commands / stderr_name, stderr.content)

        evidence = CommandEvidence(
            evidence_id=evidence_id,
            scan_id=self.scan_id,
            adapter_id=adapter_id,
            kind=EvidenceKind.VENDOR_CLI,
            captured_at=utc_now(),
            executable_path=redact_full_path(str(executable)),
            executable_size=executable_observation.size,
            executable_mtime_ns=executable_observation.mtime_ns,
            executable_volume_serial=executable_observation.volume_serial,
            executable_file_id=executable_observation.file_id,
            executable_file_id_kind=executable_observation.file_id_kind,
            executable_sha256=executable_observation.sha256,
            argv_redacted=tuple(redact_argument(argument) for argument in result.argv),
            effect_class=effect_class,
            returncode=result.returncode,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            output_limit_exceeded=result.output_limit_exceeded,
            transcript_redaction_version=TRANSCRIPT_REDACTION_VERSION,
            stdout_size=stdout.source_size,
            stderr_size=stderr.source_size,
            stdout_sha256=stdout.source_sha256,
            stderr_sha256=stderr.source_sha256,
            stdout_stored_size=stdout.stored_size,
            stderr_stored_size=stderr.stored_size,
            stdout_stored_sha256=stdout.stored_sha256,
            stderr_stored_sha256=stderr.stored_sha256,
            stdout_storage=stdout.storage,
            stderr_storage=stderr.storage,
            stdout_file=f"commands/{stdout_name}",
            stderr_file=f"commands/{stderr_name}",
        )
        metadata = (
            json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
        _atomic_write(commands / meta_name, metadata)
        return evidence

    def _observe_executable(
        self, executable: Path, *, force_hash: bool
    ) -> ExecutableObservation:
        """Hash one ordinary local executable and verify identity around the read."""

        metadata_before = read_file_metadata(executable)
        if (
            metadata_before.is_directory
            or metadata_before.is_reparse_point
            or metadata_before.is_cloud_placeholder
        ):
            raise ValueError("query executable must be an ordinary non-cloud file")
        stat_before = os.stat(executable, follow_symlinks=False)
        if stat_before.st_size < 0 or stat_before.st_size > _MAX_EXECUTABLE_HASH_BYTES:
            raise ValueError("query executable exceeds the evidence hash size limit")
        if metadata_before.logical_size != stat_before.st_size:
            raise RuntimeError("query executable size changed before evidence hashing")
        volume_serial = (
            None
            if metadata_before.volume_serial is None
            else f"{metadata_before.volume_serial:x}"
        )
        cache_key = (
            os.path.normcase(os.path.abspath(executable)),
            volume_serial,
            metadata_before.file_id,
            metadata_before.file_id_kind,
            stat_before.st_size,
            stat_before.st_mtime_ns,
        )
        cached = self._executable_cache.get(cache_key)
        if cached is not None and not force_hash:
            return cached

        digest = hashlib.sha256()
        total = 0
        with executable.open("rb") as stream:
            stream_before = os.fstat(stream.fileno())
            if _stat_identity_changed(stat_before, stream_before):
                raise RuntimeError("query executable identity changed before hashing")
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                total += len(chunk)
                if total > stat_before.st_size or total > _MAX_EXECUTABLE_HASH_BYTES:
                    raise RuntimeError("query executable grew while hashing")
                digest.update(chunk)
            stream_after = os.fstat(stream.fileno())

        metadata_after = read_file_metadata(executable)
        stat_after = os.stat(executable, follow_symlinks=False)
        if (
            total != stat_before.st_size
            or _stat_changed(stat_before, stream_after)
            or _stat_changed(stat_before, stat_after)
            or metadata_after.identity != metadata_before.identity
            or metadata_after.file_id_kind != metadata_before.file_id_kind
            or metadata_after.logical_size != metadata_before.logical_size
            or metadata_after.is_reparse_point
            or metadata_after.is_cloud_placeholder
        ):
            raise RuntimeError("query executable changed while evidence was captured")
        observation = ExecutableObservation(
            size=stat_before.st_size,
            mtime_ns=stat_before.st_mtime_ns,
            volume_serial=volume_serial,
            file_id=metadata_before.file_id,
            file_id_kind=metadata_before.file_id_kind,
            sha256=digest.hexdigest(),
        )
        self._executable_cache[cache_key] = observation
        return observation

    def record_loopback_response(
        self,
        *,
        adapter_id: str,
        endpoint: str,
        status: int,
        duration_ms: int,
        body: bytes,
        content_type: str | None,
        content_encoding: str | None,
    ) -> LoopbackEvidence:
        """Record one bounded HTTP response without persisting its source bytes."""

        return self._record_loopback(
            adapter_id=adapter_id,
            endpoint=endpoint,
            outcome=LoopbackOutcome.RESPONSE,
            http_status=status,
            error_type=None,
            duration_ms=duration_ms,
            timed_out=False,
            output_limit_exceeded=False,
            content_type_class=_classify_content_type(content_type),
            content_encoding_class=_classify_content_encoding(content_encoding),
            body=body,
        )

    def record_loopback_failure(
        self,
        *,
        adapter_id: str,
        endpoint: str,
        error_type: str,
        duration_ms: int,
        timed_out: bool,
        output_limit_exceeded: bool,
        body: bytes = b"",
        content_type: str | None = None,
        content_encoding: str | None = None,
    ) -> LoopbackEvidence:
        """Record a bounded failed observation; exception messages are never stored."""

        return self._record_loopback(
            adapter_id=adapter_id,
            endpoint=endpoint,
            outcome=LoopbackOutcome.FAILURE,
            http_status=None,
            error_type=error_type,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_limit_exceeded=output_limit_exceeded,
            content_type_class=_classify_content_type(content_type),
            content_encoding_class=_classify_content_encoding(content_encoding),
            body=body,
        )

    def _record_loopback(
        self,
        *,
        adapter_id: str,
        endpoint: str,
        outcome: LoopbackOutcome,
        http_status: int | None,
        error_type: str | None,
        duration_ms: int,
        timed_out: bool,
        output_limit_exceeded: bool,
        content_type_class: LoopbackContentTypeClass,
        content_encoding_class: LoopbackContentEncodingClass,
        body: bytes,
    ) -> LoopbackEvidence:
        if not _SAFE_ID.fullmatch(adapter_id):
            raise ValueError("adapter_id is not safe for an evidence path")
        if not isinstance(body, bytes):
            raise TypeError("loopback evidence body must be bytes")
        limit = LOOPBACK_RESPONSE_LIMITS.get(endpoint)
        if limit is None:
            raise ValueError("loopback evidence endpoint is not allowlisted")
        maximum = limit if outcome is LoopbackOutcome.RESPONSE else limit + 1
        if len(body) > maximum:
            raise ValueError("loopback evidence body exceeds its bounded endpoint limit")

        evidence_id = new_id("evidence")
        response_name = f"{evidence_id}.response.redacted.txt"
        response = redact_transcript_bytes(body)
        evidence = LoopbackEvidence(
            evidence_id=evidence_id,
            scan_id=self.scan_id,
            adapter_id=adapter_id,
            kind=EvidenceKind.LOOPBACK_API,
            captured_at=utc_now(),
            host=LOOPBACK_HOST,
            port=LOOPBACK_PORT,
            method=LOOPBACK_METHOD,
            endpoint=endpoint,
            effect_class=EffectClass.PURE_QUERY,
            outcome=outcome,
            http_status=http_status,
            error_type=error_type,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_limit_exceeded=output_limit_exceeded,
            content_type_class=content_type_class,
            content_encoding_class=content_encoding_class,
            transcript_redaction_version=TRANSCRIPT_REDACTION_VERSION,
            response_size=response.source_size,
            response_sha256=response.source_sha256,
            response_stored_size=response.stored_size,
            response_stored_sha256=response.stored_sha256,
            response_storage=response.storage,
            response_file=f"loopback/{response_name}",
        )

        # The original body remains in adapter memory. Only this redacted UTF-8
        # representation or deterministic withholding marker crosses the writer.
        loopback = self.root / "loopback"
        loopback.mkdir(parents=True, exist_ok=True)
        _atomic_write(loopback / response_name, response.content)
        metadata = (
            json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
        _atomic_write(loopback / f"{evidence_id}.meta.json", metadata)
        return evidence


def _stat_identity_changed(expected: os.stat_result, actual: os.stat_result) -> bool:
    return bool(
        (
            expected.st_dev
            and actual.st_dev
            and expected.st_dev != actual.st_dev
        )
        or (
            expected.st_ino
            and actual.st_ino
            and expected.st_ino != actual.st_ino
        )
    )


def _stat_changed(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        _stat_identity_changed(expected, actual)
        or expected.st_size != actual.st_size
        or expected.st_mtime_ns != actual.st_mtime_ns
    )


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _classify_content_type(value: str | None) -> LoopbackContentTypeClass:
    if value is None:
        return LoopbackContentTypeClass.MISSING
    if not isinstance(value, str):
        raise TypeError("content_type must be text or None")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        return LoopbackContentTypeClass.APPLICATION_JSON
    return LoopbackContentTypeClass.OTHER


def _classify_content_encoding(value: str | None) -> LoopbackContentEncodingClass:
    if value is None:
        return LoopbackContentEncodingClass.MISSING
    if not isinstance(value, str):
        raise TypeError("content_encoding must be text or None")
    if value.strip().lower() == "identity":
        return LoopbackContentEncodingClass.IDENTITY
    return LoopbackContentEncodingClass.OTHER


__all__ = ["EvidenceStore", "ExecutableObservation"]
