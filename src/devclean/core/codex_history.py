"""User-directed Codex history inventory and vendor-aware deletion.

Codex conversation rollouts are authoritative user history, not disposable
cache. DevClean therefore inventories them by recency but does not pass them to
the generic file purger. When a user explicitly chooses to delete threads, the
installed Codex runtime's ``thread/delete`` app-server method performs the
mutation so Codex can keep its indexes, projections, spawn relations, and fork
references consistent.

``history.jsonl`` is different: it is an append-only input-history file whose
records carry their own ``ts`` field. DevClean can safely prune only records
older than a user-selected cutoff with an atomic same-directory rewrite.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from devclean import __version__
from devclean.core.application_cleanup import (
    application_process_running,
    application_roots,
    clear_process_cache,
)

_ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")
_THREAD_ID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_RPC_TIMEOUT_SECONDS = 20.0


class CodexHistoryError(RuntimeError):
    """Codex history inventory or vendor action could not be completed safely."""


@dataclass(frozen=True, slots=True)
class CodexSessionEntry:
    path: Path
    thread_id: str
    updated_at: datetime
    logical_size: int
    archived: bool

    def age_days(self, now: datetime) -> float:
        return max(0.0, (now - self.updated_at).total_seconds() / 86_400)


@dataclass(frozen=True, slots=True)
class CodexAgeSummary:
    cutoff_days: int
    count: int
    logical_bytes: int


@dataclass(frozen=True, slots=True)
class CodexInputHistorySummary:
    cutoff_days: int
    removable_records: int
    removable_bytes: int
    total_records: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class CodexInputHistoryPruneResult:
    removed_records: int
    kept_records: int
    removed_bytes: int
    kept_bytes: int


@dataclass(frozen=True, slots=True)
class CodexThreadDeleteResult:
    requested: int
    deleted_or_already_absent: int
    failed: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class _RpcProcess:
    process: subprocess.Popen[str]
    messages: queue.Queue[dict[str, Any] | BaseException]
    reader: threading.Thread
    stderr_reader: threading.Thread
    stderr_lines: list[str]


PopenFactory = Callable[..., subprocess.Popen[str]]


def codex_home(environment: Mapping[str, str] | None = None) -> Path | None:
    """Resolve CODEX_HOME using the same profile rules as the cleanup catalog."""

    for root in application_roots(environment):
        if root.key == "CODEX_HOME":
            return Path(str(root.path))
    return None


def scan_codex_sessions(
    home: Path,
) -> tuple[CodexSessionEntry, ...]:
    """Inventory active and archived rollout files without reading their bodies."""

    entries: list[CodexSessionEntry] = []
    for folder_name, archived in (("sessions", False), ("archived_sessions", True)):
        root = home / folder_name
        if not root.is_dir():
            continue
        for path in root.rglob("rollout-*"):
            if not path.is_file() or not _is_rollout_path(path):
                continue
            thread_id = thread_id_from_rollout_path(path)
            if thread_id is None:
                # A rollout that cannot be tied to a Codex thread must never be
                # offered to the vendor deletion path.
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                CodexSessionEntry(
                    path=path,
                    thread_id=thread_id,
                    updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    logical_size=stat.st_size,
                    archived=archived,
                )
            )
    entries.sort(key=lambda item: item.updated_at)
    return tuple(entries)


def summarize_codex_sessions(
    sessions: Sequence[CodexSessionEntry],
    *,
    now: datetime | None = None,
    cutoffs: tuple[int, ...] = (30, 90, 180),
) -> tuple[CodexAgeSummary, ...]:
    """Return cumulative ``older than N days`` counts and reclaim estimates."""

    current = _utc(now or datetime.now(UTC))
    summaries: list[CodexAgeSummary] = []
    for cutoff in cutoffs:
        selected = [item for item in sessions if item.age_days(current) >= cutoff]
        summaries.append(
            CodexAgeSummary(
                cutoff_days=cutoff,
                count=len(selected),
                logical_bytes=sum(item.logical_size for item in selected),
            )
        )
    return tuple(summaries)


def select_codex_sessions_older_than(
    sessions: Sequence[CodexSessionEntry],
    cutoff_days: int,
    *,
    now: datetime | None = None,
) -> tuple[CodexSessionEntry, ...]:
    if cutoff_days <= 0:
        raise ValueError("cutoff_days must be positive")
    current = _utc(now or datetime.now(UTC))
    return tuple(item for item in sessions if item.age_days(current) >= cutoff_days)


def thread_id_from_rollout_path(path: Path) -> str | None:
    """Extract the canonical UUID embedded in current Codex rollout filenames."""

    match = _THREAD_ID_RE.search(path.name)
    if match is None:
        return None
    try:
        return str(uuid.UUID(match.group("id")))
    except ValueError:
        return None


def summarize_codex_input_history(
    history_path: Path,
    *,
    now: datetime | None = None,
    cutoffs: tuple[int, ...] = (30, 90, 180),
) -> tuple[CodexInputHistorySummary, ...]:
    """Inspect JSONL timestamps and estimate record-level reclaim for each cutoff."""

    current = _utc(now or datetime.now(UTC))
    records = _read_history_records(history_path)
    total_bytes = sum(len(raw) for raw, _timestamp in records)
    total_records = len(records)
    summaries: list[CodexInputHistorySummary] = []
    for cutoff in cutoffs:
        boundary = current - timedelta(days=cutoff)
        removable = [
            raw
            for raw, timestamp in records
            if timestamp is not None and timestamp <= boundary
        ]
        summaries.append(
            CodexInputHistorySummary(
                cutoff_days=cutoff,
                removable_records=len(removable),
                removable_bytes=sum(len(raw) for raw in removable),
                total_records=total_records,
                total_bytes=total_bytes,
            )
        )
    return tuple(summaries)


def prune_codex_input_history(
    history_path: Path,
    cutoff_days: int,
    *,
    now: datetime | None = None,
) -> CodexInputHistoryPruneResult:
    """Atomically remove only valid history records older than the chosen cutoff.

    Malformed/unknown records are preserved. The owning Codex application must
    be closed because it appends to the same file.
    """

    if cutoff_days <= 0:
        raise ValueError("cutoff_days must be positive")
    _require_codex_closed()
    current = _utc(now or datetime.now(UTC))
    boundary = current - timedelta(days=cutoff_days)
    records = _read_history_records(history_path)
    kept: list[bytes] = []
    removed_records = 0
    removed_bytes = 0
    for raw, timestamp in records:
        if timestamp is not None and timestamp <= boundary:
            removed_records += 1
            removed_bytes += len(raw)
        else:
            kept.append(raw)
    if removed_records == 0:
        return CodexInputHistoryPruneResult(
            removed_records=0,
            kept_records=len(records),
            removed_bytes=0,
            kept_bytes=sum(len(raw) for raw in kept),
        )

    try:
        original_stat = history_path.stat()
    except OSError as error:
        raise CodexHistoryError(f"cannot stat Codex input history: {error}") from error

    temporary = history_path.with_name(
        f".{history_path.name}.devclean-{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            for raw in kept:
                handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, original_stat.st_mode)
        os.replace(temporary, history_path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise CodexHistoryError(f"cannot rewrite Codex input history: {error}") from error

    return CodexInputHistoryPruneResult(
        removed_records=removed_records,
        kept_records=len(kept),
        removed_bytes=removed_bytes,
        kept_bytes=sum(len(raw) for raw in kept),
    )


def delete_codex_threads(
    sessions: Sequence[CodexSessionEntry],
    *,
    home: Path,
    codex_bin: Path | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
) -> CodexThreadDeleteResult:
    """Delete user-selected threads through Codex's own app-server API.

    Codex may also delete spawned descendants according to its own thread
    semantics. External fork/history references are preflighted by Codex and can
    cause an individual request to be refused. Failures are returned per thread
    instead of falling back to raw file deletion.
    """

    if not sessions:
        return CodexThreadDeleteResult(0, 0, ())
    _require_codex_closed()
    binary = codex_bin or resolve_codex_binary()
    if binary is None:
        raise CodexHistoryError(
            "Codex CLI runtime was not found; install/enable `codex` before "
            "deleting conversation history from DevClean"
        )

    rpc = _start_app_server(binary, home, popen_factory=popen_factory)
    deleted = 0
    failures: list[tuple[str, str]] = []
    try:
        _rpc_request(
            rpc,
            "initialize",
            {
                "clientInfo": {
                    "name": "devclean",
                    "title": "DevClean",
                    "version": __version__,
                },
                "capabilities": {"experimentalApi": False},
            },
            request_id="devclean-initialize",
        )
        _rpc_notify(rpc, "initialized", None)
        for index, session in enumerate(sessions):
            request_id = f"devclean-delete-{index}"
            try:
                _rpc_request(
                    rpc,
                    "thread/delete",
                    {"threadId": session.thread_id},
                    request_id=request_id,
                )
            except CodexHistoryError as error:
                # A parent thread may legitimately delete a selected spawned
                # descendant first. If the original rollout is now absent, the
                # user's desired state has already been reached.
                if not session.path.exists():
                    deleted += 1
                    continue
                failures.append((session.thread_id, str(error)))
            else:
                deleted += 1
    finally:
        _stop_app_server(rpc)
    return CodexThreadDeleteResult(
        requested=len(sessions),
        deleted_or_already_absent=deleted,
        failed=tuple(failures),
    )


def resolve_codex_binary() -> Path | None:
    """Find the user's installed Codex CLI without inventing installation paths."""

    explicit = os.environ.get("CODEX_BIN")
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
    for name in ("codex.exe", "codex"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _read_history_records(path: Path) -> list[tuple[bytes, datetime | None]]:
    try:
        raw_records = path.read_bytes().splitlines(keepends=True)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise CodexHistoryError(f"cannot read Codex input history: {error}") from error
    records: list[tuple[bytes, datetime | None]] = []
    for raw in raw_records:
        records.append((raw, _history_record_timestamp(raw)))
    return records


def _history_record_timestamp(raw: bytes) -> datetime | None:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    timestamp = payload.get("ts")
    if not isinstance(timestamp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _is_rollout_path(path: Path) -> bool:
    lower = path.name.casefold()
    return lower.endswith(_ROLLOUT_SUFFIXES)


def _require_codex_closed() -> None:
    clear_process_cache()
    if application_process_running("codex"):
        raise CodexHistoryError(
            "Codex/ChatGPT is running; close it before changing Codex history"
        )


def _start_app_server(
    binary: Path,
    home: Path,
    *,
    popen_factory: PopenFactory,
) -> _RpcProcess:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(home)
    try:
        process = popen_factory(
            [str(binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
    except OSError as error:
        raise CodexHistoryError(f"cannot start Codex app-server: {error}") from error
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise CodexHistoryError("Codex app-server did not expose stdio pipes")

    messages: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
    stderr_lines: list[str] = []
    reader = threading.Thread(
        target=_read_rpc_lines,
        args=(process.stdout, messages),
        name="DevClean-Codex-RPC",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=_read_stderr_lines,
        args=(process.stderr, stderr_lines),
        name="DevClean-Codex-stderr",
        daemon=True,
    )
    reader.start()
    stderr_reader.start()
    return _RpcProcess(process, messages, reader, stderr_reader, stderr_lines)


def _stop_app_server(rpc: _RpcProcess) -> None:
    process = rpc.process
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    rpc.reader.join(timeout=0.5)
    rpc.stderr_reader.join(timeout=0.5)


def _read_rpc_lines(
    stream: TextIO,
    messages: queue.Queue[dict[str, Any] | BaseException],
) -> None:
    try:
        for line in stream:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                messages.put(message)
    except BaseException as error:
        messages.put(error)


def _read_stderr_lines(stream: TextIO, target: list[str]) -> None:
    try:
        for line in stream:
            target.append(line.rstrip())
            if len(target) > 40:
                del target[:-40]
    except OSError:
        return


def _rpc_write(rpc: _RpcProcess, message: dict[str, Any]) -> None:
    stdin = rpc.process.stdin
    if stdin is None:
        raise CodexHistoryError("Codex app-server stdin is closed")
    try:
        stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        stdin.flush()
    except (BrokenPipeError, OSError) as error:
        raise CodexHistoryError(
            _rpc_failure_text(rpc, f"Codex app-server transport failed: {error}")
        ) from error


def _rpc_notify(
    rpc: _RpcProcess,
    method: str,
    params: dict[str, Any] | None,
) -> None:
    message: dict[str, Any] = {"method": method}
    if params is not None:
        message["params"] = params
    _rpc_write(rpc, message)


def _rpc_request(
    rpc: _RpcProcess,
    method: str,
    params: dict[str, Any] | None,
    *,
    request_id: str,
) -> Any:
    message: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    _rpc_write(rpc, message)
    while True:
        try:
            incoming = rpc.messages.get(timeout=_RPC_TIMEOUT_SECONDS)
        except queue.Empty as error:
            raise CodexHistoryError(
                _rpc_failure_text(rpc, f"Codex app-server timed out during {method}")
            ) from error
        if isinstance(incoming, BaseException):
            raise CodexHistoryError(
                _rpc_failure_text(rpc, f"Codex app-server reader failed: {incoming}")
            ) from incoming
        if incoming.get("id") != request_id:
            # Notifications, including thread/deleted, are informational here.
            # No server-initiated request is expected for initialize/delete.
            continue
        if "error" in incoming:
            error_payload = incoming.get("error")
            if isinstance(error_payload, dict):
                message_text = str(error_payload.get("message", error_payload))
            else:
                message_text = str(error_payload)
            raise CodexHistoryError(f"Codex refused {method}: {message_text}")
        return incoming.get("result")


def _rpc_failure_text(rpc: _RpcProcess, message: str) -> str:
    if not rpc.stderr_lines:
        return message
    return f"{message}; Codex stderr: {rpc.stderr_lines[-1]}"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "CodexAgeSummary",
    "CodexHistoryError",
    "CodexInputHistoryPruneResult",
    "CodexInputHistorySummary",
    "CodexSessionEntry",
    "CodexThreadDeleteResult",
    "codex_home",
    "delete_codex_threads",
    "prune_codex_input_history",
    "resolve_codex_binary",
    "scan_codex_sessions",
    "select_codex_sessions_older_than",
    "summarize_codex_input_history",
    "summarize_codex_sessions",
    "thread_id_from_rollout_path",
]
