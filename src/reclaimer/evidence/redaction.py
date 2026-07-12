"""Conservative, bounded redaction for reports and persisted transcripts.

The command parser deliberately receives the original in-memory bytes.  This
module is the separate persistence boundary: it either returns UTF-8 content
that has passed conservative redaction, or a small marker.  It never falls
back to returning undecoded or partially processed source bytes.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:access[_-]?token|auth(?:orization)?|credential|cookie|"
    r"password|passwd|proxy[_-]?authorization|registry[_-]?auth|secret|session|"
    r"token|api[_-]?key|user(?:name)?|login)\b[\"']?\s*[=:]\s*)"
    r"(?:[\"'][^\r\n]*?[\"']|[^\s,;&}\]]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO = re.compile(r"(https?://)([^/@\s:]+):([^/@\s]+)@", re.IGNORECASE)
_URL = re.compile(r"(?i)\b(?:https?|ftp|ssh|git|file)://[^\s<>\"']+")
_EMAIL = re.compile(r"(?i)(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_REMOTE_LOCATOR = re.compile(r"(?i)(?<![\w.-])[\w.-]+@[\w.-]+:[^\s,;]+")
_SENSITIVE_HEADER = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"hf_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")(?![A-Za-z0-9])"
)
_OPAQUE_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9._~+/-]{24,}={0,2}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9._~+/-]*[A-Za-z])(?=[A-Za-z0-9._~+/-]*[0-9])"
    r"[A-Za-z0-9._~+/-]{24,}={0,2}(?![A-Za-z0-9])"
)
_SENSITIVE_WORD = re.compile(
    r"(?i)\b(?:access[_-]?token|authorization|credential|password|passwd|"
    r"proxy[_-]?authorization|registry[_-]?auth|secret|session[_-]?key|token|"
    r"api[_-]?key)\b"
)
_WINDOWS_USER = re.compile(r"(?i)([A-Z]:\\Users\\)([^\\/]+)")
_POSIX_USER = re.compile(r"(?i)(/(?:home|Users)/)([^/\s]+)")
_WINDOWS_ABSOLUTE = re.compile(r"(?i)^([A-Z]):[\\/]")
_WINDOWS_ABSOLUTE_ANY = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\s,;]+")
_POSIX_ABSOLUTE_ANY = re.compile(r"(?<![A-Za-z0-9:])/(?:[^/\s,;]+/)*[^\s,;]*")

MAX_TRANSCRIPT_SOURCE_BYTES: Final = 4 * 1024 * 1024
MAX_TRANSCRIPT_STORED_BYTES: Final = 4 * 1024 * 1024
MAX_REDACTED_ARGUMENT_CHARS: Final = 32_767
TRANSCRIPT_REDACTION_VERSION: Final = "transcript-redaction-v1"


class TranscriptStorage(StrEnum):
    """How a command stream was represented in its persisted evidence file."""

    REDACTED_UTF8 = "REDACTED_UTF8"
    NON_UTF8_MARKER = "NON_UTF8_MARKER"
    UNSAFE_TEXT_MARKER = "UNSAFE_TEXT_MARKER"
    SOURCE_TOO_LARGE_MARKER = "SOURCE_TOO_LARGE_MARKER"
    REDACTION_ERROR_MARKER = "REDACTION_ERROR_MARKER"
    REDACTED_OUTPUT_TOO_LARGE_MARKER = "REDACTED_OUTPUT_TOO_LARGE_MARKER"


@dataclass(frozen=True, slots=True)
class RedactedTranscript:
    """A safe-to-persist transcript plus identities for source and stored bytes."""

    content: bytes
    storage: TranscriptStorage
    source_size: int
    source_sha256: str
    stored_size: int
    stored_sha256: str


def redact_path(value: str, home: Path | None = None) -> str:
    """Redact the current home directory and Windows user-profile segment."""

    result = value
    home_path = home or Path.home()
    home_text = str(home_path)
    if home_text:
        result = re.sub(re.escape(home_text), r"<USER_HOME>", result, flags=re.IGNORECASE)
    result = _WINDOWS_USER.sub(r"\1<USER>", result)
    return _POSIX_USER.sub(r"\1<USER>", result)


def redact_text(value: str, home: Path | None = None) -> str:
    """Redact common credential forms and user-profile paths.

    This is defense in depth, not a general secret scanner. Callers must avoid collecting secrets
    in the first place.
    """

    return redact_secrets(redact_path(value, home=home))


def redact_secrets(value: str) -> str:
    """Redact credential-shaped text while preserving local paths."""

    result = value
    result = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}<REDACTED>", result)
    result = _BEARER.sub("Bearer <REDACTED>", result)
    result = _URL_USERINFO.sub(r"\1<REDACTED>@", result)
    result = _KNOWN_TOKEN.sub("<REDACTED_TOKEN>", result)
    return _OPAQUE_TOKEN.sub("<REDACTED_OPAQUE>", result)


def redact_full_path(value: str) -> str:
    """Replace an absolute locator while retaining only its broad path class."""

    result = redact_secrets(value)
    if result.startswith((r"\\", "//")):
        return "<NETWORK_PATH>"
    if result.startswith("/"):
        return "<ABSOLUTE_PATH>"
    match = _WINDOWS_ABSOLUTE.match(result)
    if match:
        return f"{match.group(1).upper()}:\\<REDACTED_PATH>"
    if Path(result).is_absolute():
        return "<ABSOLUTE_PATH>"
    return redact_text(result)


def redact_argument(value: str) -> str:
    """Broadly redact one persisted argv item, including embedded absolute paths.

    Arguments are metadata, not parser input.  Losing path detail here is an
    intentional privacy tradeoff; the executable's file identity is recorded
    separately by size and mtime.
    """

    if len(value) > MAX_REDACTED_ARGUMENT_CHARS or _has_unsafe_text(value):
        return "<UNSAFE_ARGUMENT>"
    try:
        value.encode("utf-8", errors="strict")
        result = _redact_transcript_text(value)
    except Exception:
        # Metadata redaction must fail closed even for an unexpected bug in a
        # future redactor revision.  BaseException is intentionally not caught.
        return "<UNSAFE_ARGUMENT>"

    if _is_absolute_path(result):
        return redact_full_path(result)

    option, separator, candidate = result.partition("=")
    if separator and _is_absolute_path(candidate):
        return f"{option}=<ABSOLUTE_PATH>"
    result = _WINDOWS_ABSOLUTE_ANY.sub("<ABSOLUTE_PATH>", result)
    return _POSIX_ABSOLUTE_ANY.sub("<ABSOLUTE_PATH>", result)


def redact_transcript_bytes(source: bytes) -> RedactedTranscript:
    """Return a bounded safe representation without ever persisting ``source``.

    Strict UTF-8 is the only accepted transcript encoding in v1.  Binary,
    control-bearing, oversized, or unexpectedly unredactable data becomes a
    deterministic marker that contains only the original byte count and hash.
    """

    source_size = len(source)
    source_sha256 = hashlib.sha256(source).hexdigest()
    if source_size > MAX_TRANSCRIPT_SOURCE_BYTES:
        return _marker(TranscriptStorage.SOURCE_TOO_LARGE_MARKER, source_size, source_sha256)
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _marker(TranscriptStorage.NON_UTF8_MARKER, source_size, source_sha256)
    if _has_unsafe_text(text):
        return _marker(TranscriptStorage.UNSAFE_TEXT_MARKER, source_size, source_sha256)
    try:
        stored = _redact_transcript_text(text).encode("utf-8", errors="strict")
    except Exception:
        # No exception from the redaction pipeline may make source bytes the
        # persistence fallback.  BaseException is intentionally not caught.
        return _marker(TranscriptStorage.REDACTION_ERROR_MARKER, source_size, source_sha256)
    if len(stored) > MAX_TRANSCRIPT_STORED_BYTES:
        return _marker(
            TranscriptStorage.REDACTED_OUTPUT_TOO_LARGE_MARKER,
            source_size,
            source_sha256,
        )
    return _result(stored, TranscriptStorage.REDACTED_UTF8, source_size, source_sha256)


def _redact_transcript_text(value: str) -> str:
    result = redact_text(value)
    result = _SENSITIVE_HEADER.sub(lambda match: f"{match.group(1)}<REDACTED>", result)
    result = _URL.sub("<REDACTED_URL>", result)
    result = _REMOTE_LOCATOR.sub("<REDACTED_REMOTE>", result)
    result = _EMAIL.sub("<REDACTED_EMAIL>", result)

    # A credential-bearing line that did not match a known value shape is not
    # safe to retain.  Redact the whole line instead of guessing its grammar.
    lines = result.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if _SENSITIVE_WORD.search(line) and "<REDACTED>" not in line:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            lines[index] = f"<REDACTED_SENSITIVE_LINE>{ending}"
    return "".join(lines)


def _has_unsafe_text(value: str) -> bool:
    for character in value:
        if character in "\t\r\n":
            continue
        codepoint = ord(character)
        if unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co"}:
            return True
        if codepoint & 0xFFFF in {0xFFFE, 0xFFFF}:
            return True
    return False


def _is_absolute_path(value: str) -> bool:
    return (
        bool(_WINDOWS_ABSOLUTE.match(value))
        or value.startswith((r"\\", "//", "/"))
        or Path(value).is_absolute()
    )


def _marker(storage: TranscriptStorage, source_size: int, source_sha256: str) -> RedactedTranscript:
    content = (
        "[RECLAIMER_TRANSCRIPT_WITHHELD "
        f"reason={storage.value} source_size={source_size} source_sha256={source_sha256}]\n"
    ).encode("ascii")
    return _result(content, storage, source_size, source_sha256)


def _result(
    content: bytes,
    storage: TranscriptStorage,
    source_size: int,
    source_sha256: str,
) -> RedactedTranscript:
    return RedactedTranscript(
        content=content,
        storage=storage,
        source_size=source_size,
        source_sha256=source_sha256,
        stored_size=len(content),
        stored_sha256=hashlib.sha256(content).hexdigest(),
    )


__all__ = [
    "MAX_TRANSCRIPT_SOURCE_BYTES",
    "TRANSCRIPT_REDACTION_VERSION",
    "RedactedTranscript",
    "TranscriptStorage",
    "redact_argument",
    "redact_full_path",
    "redact_path",
    "redact_secrets",
    "redact_text",
    "redact_transcript_bytes",
]
