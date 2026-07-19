"""Bounded, fail-closed Windows directory change hints.

``ReadDirectoryChangesW`` notifications are treated only as invalidation hints.
They are not a durable journal, a filesystem snapshot, or authority to skip a
directory or delete a path.  A stopped, overflowing, malformed, or otherwise
unhealthy monitor always requires an ordinary full scan.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import re
import struct
from collections import deque
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from threading import Event, Lock, Thread, current_thread
from typing import Protocol
from uuid import uuid4

from devclean.core.incremental import FallbackReason
from devclean.platform.windows.filesystem import (
    FileSystemMetadata,
    read_file_metadata,
    read_file_metadata_handle,
)

_DEFAULT_BUFFER_SIZE = 64 * 1024
_MIN_BUFFER_SIZE = 4096
_MAX_BUFFER_SIZE = 64 * 1024
_DEFAULT_MAX_PENDING_HINTS = 65_536
_MAX_PENDING_HINTS = 1_000_000
_MAX_PATH_TEXT = 32_767
_MAX_DETAIL_TEXT = 2048
_SESSION_TOKEN_PATTERN = re.compile(r"^session_[a-f0-9]{32}$")

_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL = 0x00100000
_FILE_FLAG_OVERLAPPED = 0x40000000

_FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
_FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
_FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x00000004
_FILE_NOTIFY_CHANGE_SIZE = 0x00000008
_FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
_FILE_NOTIFY_CHANGE_CREATION = 0x00000040
_FILE_NOTIFY_CHANGE_SECURITY = 0x00000100
_NOTIFY_FILTER = (
    _FILE_NOTIFY_CHANGE_FILE_NAME
    | _FILE_NOTIFY_CHANGE_DIR_NAME
    | _FILE_NOTIFY_CHANGE_ATTRIBUTES
    | _FILE_NOTIFY_CHANGE_SIZE
    | _FILE_NOTIFY_CHANGE_LAST_WRITE
    | _FILE_NOTIFY_CHANGE_CREATION
    | _FILE_NOTIFY_CHANGE_SECURITY
)

_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_HANDLE = 6
_ERROR_NOT_READY = 21
_ERROR_OPERATION_ABORTED = 995
_ERROR_IO_PENDING = 997
_ERROR_NOTIFY_ENUM_DIR = 1022
_ERROR_DEVICE_NOT_CONNECTED = 1167
_HANDLE_ERROR_CODES = frozenset(
    {
        _ERROR_FILE_NOT_FOUND,
        _ERROR_PATH_NOT_FOUND,
        _ERROR_ACCESS_DENIED,
        _ERROR_INVALID_HANDLE,
        _ERROR_NOT_READY,
        _ERROR_DEVICE_NOT_CONNECTED,
    }
)

_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class ChangeAction(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    MODIFIED = "MODIFIED"
    RENAMED_OLD_NAME = "RENAMED_OLD_NAME"
    RENAMED_NEW_NAME = "RENAMED_NEW_NAME"


_ACTION_BY_WIN32_VALUE = {
    1: ChangeAction.ADDED,
    2: ChangeAction.REMOVED,
    3: ChangeAction.MODIFIED,
    4: ChangeAction.RENAMED_OLD_NAME,
    5: ChangeAction.RENAMED_NEW_NAME,
}


class MonitorState(StrEnum):
    NEW = "NEW"
    STARTING = "STARTING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    STOPPED = "STOPPED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ChangeHint:
    """One path that must be conservatively re-observed."""

    sequence: int
    action: ChangeAction
    relative_path: str

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("change sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("change sequence must be positive")
        if not isinstance(self.action, ChangeAction):
            raise ValueError("change action must be a ChangeAction")
        normalized = _normalize_relative_path(self.relative_path)
        if normalized != self.relative_path:
            raise ValueError("change path must already be normalized")


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    """A bounded snapshot of pending hints from one live session."""

    session_token: str
    root: str
    state: MonitorState
    from_sequence: int
    through_sequence: int
    acknowledged_sequence: int
    hints: tuple[ChangeHint, ...]
    fallback_reason: FallbackReason | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if _SESSION_TOKEN_PATTERN.fullmatch(self.session_token) is None:
            raise ValueError("change batch session token is invalid")
        if not isinstance(self.state, MonitorState):
            raise ValueError("change batch state must be a MonitorState")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason, FallbackReason
        ):
            raise ValueError("change batch fallback must be a FallbackReason")
        for name in ("from_sequence", "through_sequence", "acknowledged_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.from_sequence > self.through_sequence:
            raise ValueError("change batch sequence range is reversed")
        if self.acknowledged_sequence > self.through_sequence:
            raise ValueError("acknowledged sequence is ahead of monitor sequence")
        if any(
            not self.from_sequence < hint.sequence <= self.through_sequence for hint in self.hints
        ):
            raise ValueError("change hint lies outside its batch sequence range")
        if self.detail is not None and len(self.detail) > _MAX_DETAIL_TEXT:
            raise ValueError("change batch detail is too long")
        # Token/sequence validation can fail even while the underlying monitor
        # remains healthy, so those three caller-specific reasons are allowed.
        if (
            self.state is MonitorState.ACTIVE
            and self.fallback_reason is not None
            and self.fallback_reason
            not in {
                FallbackReason.SESSION_TOKEN_MISMATCH,
                FallbackReason.SESSION_SEQUENCE_GAP,
                FallbackReason.SESSION_SEQUENCE_REGRESSION,
            }
        ):
            raise ValueError("active monitor has an incompatible failure reason")
        if self.state is not MonitorState.ACTIVE and self.fallback_reason is None:
            raise ValueError("inactive monitor snapshots must require a full scan")

    @property
    def requires_full_scan(self) -> bool:
        return self.fallback_reason is not None or self.state is not MonitorState.ACTIVE


class _NotificationParseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _BackendCallbacks:
    root: str
    buffer_size: int
    expected_root_identity: tuple[int, str]
    stop_event: Event
    ready: Callable[[], None]
    publish: Callable[[ChangeAction, str], bool]
    fail: Callable[[FallbackReason, str], None]


class _MonitorBackend(Protocol):
    def run(self, callbacks: _BackendCallbacks) -> None: ...

    def request_stop(self) -> None: ...


class DirectoryChangeMonitor:
    """One non-persistent ``ReadDirectoryChangesW`` monitoring session."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        max_pending_hints: int = _DEFAULT_MAX_PENDING_HINTS,
        _backend: _MonitorBackend | None = None,
        _platform_supported: bool | None = None,
    ) -> None:
        if (
            isinstance(buffer_size, bool)
            or not isinstance(buffer_size, int)
            or not _MIN_BUFFER_SIZE <= buffer_size <= _MAX_BUFFER_SIZE
            or buffer_size % 4 != 0
        ):
            raise ValueError("buffer_size must be a DWORD-aligned value from 4096 to 65536")
        if (
            isinstance(max_pending_hints, bool)
            or not isinstance(max_pending_hints, int)
            or not 1 <= max_pending_hints <= _MAX_PENDING_HINTS
        ):
            raise ValueError("max_pending_hints is outside its safe bound")

        self.root = os.path.abspath(os.fspath(Path(root)))
        if not self.root or len(self.root) > _MAX_PATH_TEXT:
            raise ValueError("monitor root must be a bounded absolute path")
        self.buffer_size = buffer_size
        self.max_pending_hints = max_pending_hints
        self.session_token = f"session_{uuid4().hex}"

        self._platform_supported = (
            os.name == "nt" if _platform_supported is None else _platform_supported
        )
        self._backend: _MonitorBackend = _backend or _WindowsChangeBackend()
        self._lock = Lock()
        self._ready = Event()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._state = MonitorState.NEW
        self._fallback_reason: FallbackReason | None = None
        self._detail: str | None = None
        self._root_identity: tuple[int, str] | None = None
        self._hints: deque[ChangeHint] = deque()
        self._next_sequence = 0
        self._acknowledged_sequence = 0

    @property
    def state(self) -> MonitorState:
        with self._lock:
            return self._state

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    @property
    def acknowledged_sequence(self) -> int:
        with self._lock:
            return self._acknowledged_sequence

    def start(self) -> DirectoryChangeMonitor:
        """Start monitoring, or publish an explicit unsupported result."""

        with self._lock:
            if self._state is not MonitorState.NEW:
                raise RuntimeError("change monitor can only be started once")
            self._state = MonitorState.STARTING

        if not self._platform_supported:
            self._set_terminal(
                MonitorState.UNSUPPORTED,
                FallbackReason.UNSUPPORTED_PLATFORM,
                "ReadDirectoryChangesW is available only on Windows",
                request_backend_stop=False,
            )
            return self

        try:
            metadata = read_file_metadata(self.root)
        except OSError as error:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.ROOT_CHANGED,
                _bounded_detail(f"monitor root cannot be observed: {error}"),
                request_backend_stop=False,
            )
            return self
        reason = _root_metadata_failure(metadata)
        if reason is not None:
            self._set_terminal(
                MonitorState.FAILED,
                reason,
                "monitor root is not an ordinary directory with stable identity",
                request_backend_stop=False,
            )
            return self
        assert metadata.identity is not None
        self._root_identity = metadata.identity

        self._thread = Thread(
            target=self._run_backend,
            name="DevClean-directory-change-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Wait until the session is ACTIVE or has failed closed."""

        return self._ready.wait(timeout)

    def read_changes(self, *, session_token: str, after_sequence: int) -> ChangeBatch:
        """Return pending invalidation hints without acknowledging them."""

        _require_sequence(after_sequence, "after_sequence")
        if session_token != self.session_token:
            return self._batch_with_caller_failure(
                FallbackReason.SESSION_TOKEN_MISMATCH,
                "checkpoint belongs to another monitor session",
            )

        if self.state is MonitorState.ACTIVE:
            self._revalidate_root()

        with self._lock:
            state = self._state
            next_sequence = self._next_sequence
            acknowledged = self._acknowledged_sequence
            reason = self._fallback_reason
            detail = self._detail
            if state in {MonitorState.NEW, MonitorState.STARTING}:
                reason = FallbackReason.MONITOR_NOT_READY
                detail = "monitor has not reached its active state"
            if after_sequence < acknowledged:
                reason = FallbackReason.SESSION_SEQUENCE_GAP
                detail = "requested sequence has already been acknowledged"
                batch_start = acknowledged
                hints: tuple[ChangeHint, ...] = ()
            elif after_sequence > next_sequence:
                reason = FallbackReason.SESSION_SEQUENCE_REGRESSION
                detail = "requested sequence is ahead of the live monitor"
                batch_start = next_sequence
                hints = ()
            else:
                batch_start = after_sequence
                hints = tuple(hint for hint in self._hints if hint.sequence > after_sequence)
            return ChangeBatch(
                session_token=self.session_token,
                root=self.root,
                state=state,
                from_sequence=batch_start,
                through_sequence=next_sequence,
                acknowledged_sequence=acknowledged,
                hints=hints,
                fallback_reason=reason,
                detail=detail,
            )

    def acknowledge(self, *, session_token: str, through_sequence: int) -> FallbackReason | None:
        """Drop hints only after their shadow generation has committed."""

        _require_sequence(through_sequence, "through_sequence")
        if session_token != self.session_token:
            return FallbackReason.SESSION_TOKEN_MISMATCH
        with self._lock:
            if self._state is not MonitorState.ACTIVE:
                return self._fallback_reason or FallbackReason.MONITOR_STOPPED
            if through_sequence < self._acknowledged_sequence:
                return FallbackReason.SESSION_SEQUENCE_GAP
            if through_sequence > self._next_sequence:
                return FallbackReason.SESSION_SEQUENCE_REGRESSION
            while self._hints and self._hints[0].sequence <= through_sequence:
                self._hints.popleft()
            self._acknowledged_sequence = through_sequence
        return None

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the watcher; stopped sessions are never reusable."""

        if timeout < 0:
            raise ValueError("stop timeout cannot be negative")
        self._stop_event.set()
        self._backend.request_stop()
        thread = self._thread
        if thread is not None and thread is not current_thread():
            thread.join(timeout)
        if thread is not None and thread.is_alive():
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.MONITOR_STOP_TIMEOUT,
                "monitor worker did not stop within its bound",
                request_backend_stop=False,
            )
            return
        self._set_stopped()

    def __enter__(self) -> DirectoryChangeMonitor:
        if self.state is MonitorState.NEW:
            self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()

    def _run_backend(self) -> None:
        identity = self._root_identity
        if identity is None:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.ROOT_IDENTITY_UNAVAILABLE,
                "monitor root identity was lost before startup",
            )
            return
        callbacks = _BackendCallbacks(
            root=self.root,
            buffer_size=self.buffer_size,
            expected_root_identity=identity,
            stop_event=self._stop_event,
            ready=self._mark_active,
            publish=self._publish,
            fail=self._backend_failure,
        )
        try:
            self._backend.run(callbacks)
        except BaseException as error:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.MONITOR_IO_ERROR,
                _bounded_detail(f"monitor backend failed: {type(error).__name__}: {error}"),
                request_backend_stop=False,
            )
        finally:
            self._set_stopped()

    def _mark_active(self) -> None:
        with self._lock:
            if self._state is MonitorState.STARTING:
                self._state = MonitorState.ACTIVE
                self._ready.set()

    def _publish(self, action: ChangeAction, relative_path: str) -> bool:
        try:
            normalized = _normalize_relative_path(relative_path)
        except ValueError as error:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.MALFORMED_NOTIFICATION,
                _bounded_detail(str(error)),
            )
            return False

        capacity_exceeded = False
        with self._lock:
            if self._state is not MonitorState.ACTIVE:
                return False
            if len(self._hints) >= self.max_pending_hints:
                capacity_exceeded = True
            else:
                self._next_sequence += 1
                self._hints.append(
                    ChangeHint(
                        sequence=self._next_sequence,
                        action=action,
                        relative_path=normalized,
                    )
                )
        if capacity_exceeded:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.MONITOR_CAPACITY_EXCEEDED,
                "pending change hint capacity was exceeded",
            )
            return False
        return True

    def _backend_failure(self, reason: FallbackReason, detail: str) -> None:
        self._set_terminal(
            MonitorState.FAILED,
            reason,
            _bounded_detail(detail),
            request_backend_stop=False,
        )

    def _revalidate_root(self) -> None:
        try:
            metadata = read_file_metadata(self.root)
        except OSError as error:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.ROOT_CHANGED,
                _bounded_detail(f"monitor root can no longer be observed: {error}"),
            )
            return
        if _root_metadata_failure(metadata) is not None or metadata.identity != self._root_identity:
            self._set_terminal(
                MonitorState.FAILED,
                FallbackReason.ROOT_CHANGED,
                "monitor root identity or boundary state changed",
            )

    def _batch_with_caller_failure(self, reason: FallbackReason, detail: str) -> ChangeBatch:
        with self._lock:
            next_sequence = self._next_sequence
            return ChangeBatch(
                session_token=self.session_token,
                root=self.root,
                state=self._state,
                from_sequence=next_sequence,
                through_sequence=next_sequence,
                acknowledged_sequence=self._acknowledged_sequence,
                hints=(),
                fallback_reason=reason,
                detail=detail,
            )

    def _set_terminal(
        self,
        state: MonitorState,
        reason: FallbackReason,
        detail: str,
        *,
        request_backend_stop: bool = True,
    ) -> None:
        changed = False
        with self._lock:
            if self._state not in {
                MonitorState.FAILED,
                MonitorState.STOPPED,
                MonitorState.UNSUPPORTED,
            }:
                self._state = state
                self._fallback_reason = reason
                self._detail = _bounded_detail(detail)
                self._stop_event.set()
                self._ready.set()
                changed = True
        if changed and request_backend_stop:
            self._backend.request_stop()

    def _set_stopped(self) -> None:
        with self._lock:
            if self._state in {MonitorState.FAILED, MonitorState.UNSUPPORTED}:
                self._ready.set()
                return
            if self._state is not MonitorState.STOPPED:
                self._state = MonitorState.STOPPED
                self._fallback_reason = FallbackReason.MONITOR_STOPPED
                self._detail = "monitor session has stopped"
                self._ready.set()


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _WindowsChangeBackend:
    def __init__(self) -> None:
        self._lock = Lock()
        self._handle: wintypes.HANDLE | None = None

    def request_stop(self) -> None:
        if os.name != "nt":
            return
        with self._lock:
            handle = self._handle
        if handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        cancel_io = kernel32.CancelIoEx
        cancel_io.argtypes = [wintypes.HANDLE, ctypes.POINTER(_OVERLAPPED)]
        cancel_io.restype = wintypes.BOOL
        cancel_io(handle, None)

    def run(self, callbacks: _BackendCallbacks) -> None:
        if os.name != "nt":
            callbacks.fail(
                FallbackReason.UNSUPPORTED_PLATFORM,
                "Windows change backend was invoked on another platform",
            )
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        create_event = kernel32.CreateEventW
        create_event.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        create_event.restype = wintypes.HANDLE
        reset_event = kernel32.ResetEvent
        reset_event.argtypes = [wintypes.HANDLE]
        reset_event.restype = wintypes.BOOL
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        read_changes = kernel32.ReadDirectoryChangesW
        read_changes.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            ctypes.POINTER(_OVERLAPPED),
            wintypes.LPVOID,
        ]
        read_changes.restype = wintypes.BOOL
        get_overlapped_result = kernel32.GetOverlappedResult
        get_overlapped_result.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_OVERLAPPED),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        ]
        get_overlapped_result.restype = wintypes.BOOL

        handle = create_file(
            callbacks.root,
            _FILE_LIST_DIRECTORY,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS
            | _FILE_FLAG_OPEN_REPARSE_POINT
            | _FILE_FLAG_OPEN_NO_RECALL
            | _FILE_FLAG_OVERLAPPED,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error_code = ctypes.get_last_error()
            callbacks.fail(
                _fallback_for_windows_error(error_code),
                f"CreateFileW failed with Windows error {error_code}",
            )
            return

        event = create_event(None, True, False, None)
        if not event:
            error_code = ctypes.get_last_error()
            close_handle(handle)
            callbacks.fail(
                FallbackReason.MONITOR_IO_ERROR,
                f"CreateEventW failed with Windows error {error_code}",
            )
            return

        with self._lock:
            self._handle = handle
        try:
            try:
                opened_metadata = read_file_metadata_handle(handle)
            except OSError as error:
                callbacks.fail(
                    FallbackReason.MONITOR_HANDLE_LOST,
                    f"opened monitor root cannot be identified: {error}",
                )
                return
            if (
                _root_metadata_failure(opened_metadata) is not None
                or opened_metadata.identity != callbacks.expected_root_identity
            ):
                callbacks.fail(
                    FallbackReason.ROOT_CHANGED,
                    "monitor root changed between validation and handle open",
                )
                return

            callbacks.ready()
            buffer = ctypes.create_string_buffer(callbacks.buffer_size)
            while not callbacks.stop_event.is_set():
                if not reset_event(event):
                    error_code = ctypes.get_last_error()
                    callbacks.fail(
                        FallbackReason.MONITOR_IO_ERROR,
                        f"ResetEvent failed with Windows error {error_code}",
                    )
                    return
                overlapped = _OVERLAPPED()
                overlapped.hEvent = event
                started = read_changes(
                    handle,
                    buffer,
                    callbacks.buffer_size,
                    True,
                    _NOTIFY_FILTER,
                    None,
                    ctypes.byref(overlapped),
                    None,
                )
                if not started:
                    error_code = ctypes.get_last_error()
                    if error_code != _ERROR_IO_PENDING:
                        callbacks.fail(
                            _fallback_for_windows_error(error_code),
                            f"ReadDirectoryChangesW failed with Windows error {error_code}",
                        )
                        return

                wait_result = _WAIT_TIMEOUT
                while wait_result == _WAIT_TIMEOUT and not callbacks.stop_event.is_set():
                    wait_result = int(wait_for_single_object(event, 250))
                if callbacks.stop_event.is_set():
                    self.request_stop()
                    wait_for_single_object(event, 1000)
                    return
                if wait_result == _WAIT_FAILED:
                    error_code = ctypes.get_last_error()
                    callbacks.fail(
                        FallbackReason.MONITOR_IO_ERROR,
                        f"WaitForSingleObject failed with Windows error {error_code}",
                    )
                    return
                if wait_result != _WAIT_OBJECT_0:
                    callbacks.fail(
                        FallbackReason.MONITOR_IO_ERROR,
                        f"unexpected wait result {wait_result}",
                    )
                    return

                transferred = wintypes.DWORD()
                if not get_overlapped_result(
                    handle, ctypes.byref(overlapped), ctypes.byref(transferred), False
                ):
                    error_code = ctypes.get_last_error()
                    if error_code == _ERROR_OPERATION_ABORTED and callbacks.stop_event.is_set():
                        return
                    callbacks.fail(
                        _fallback_for_windows_error(error_code),
                        f"GetOverlappedResult failed with Windows error {error_code}",
                    )
                    return
                byte_count = int(transferred.value)
                if byte_count == 0:
                    callbacks.fail(
                        FallbackReason.MONITOR_BUFFER_OVERFLOW,
                        "ReadDirectoryChangesW discarded an overflowing buffer",
                    )
                    return
                try:
                    notifications = _parse_notification_buffer(
                        bytes(buffer.raw[:byte_count]), byte_count
                    )
                except _NotificationParseError as error:
                    callbacks.fail(
                        FallbackReason.MALFORMED_NOTIFICATION,
                        str(error),
                    )
                    return
                for action, relative_path in notifications:
                    if not callbacks.publish(action, relative_path):
                        return
        finally:
            with self._lock:
                self._handle = None
            close_handle(event)
            close_handle(handle)


def _fallback_for_windows_error(error_code: int) -> FallbackReason:
    if error_code == _ERROR_NOTIFY_ENUM_DIR:
        return FallbackReason.MONITOR_BUFFER_OVERFLOW
    if error_code in _HANDLE_ERROR_CODES:
        return FallbackReason.MONITOR_HANDLE_LOST
    return FallbackReason.MONITOR_IO_ERROR


def _parse_notification_buffer(
    buffer: bytes, byte_count: int
) -> tuple[tuple[ChangeAction, str], ...]:
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 12
        or byte_count > len(buffer)
    ):
        raise _NotificationParseError("notification buffer length is invalid")

    notifications: list[tuple[ChangeAction, str]] = []
    offset = 0
    while True:
        if offset + 12 > byte_count:
            raise _NotificationParseError("notification header is truncated")
        next_offset, action_value, name_length = struct.unpack_from("<III", buffer, offset)
        action = _ACTION_BY_WIN32_VALUE.get(action_value)
        if action is None:
            raise _NotificationParseError("notification action is unsupported")
        if name_length == 0 or name_length % 2 != 0:
            raise _NotificationParseError("notification name length is invalid")
        record_end = byte_count if next_offset == 0 else offset + next_offset
        minimum_end = offset + 12 + name_length
        if record_end > byte_count or minimum_end > record_end:
            raise _NotificationParseError("notification record exceeds its buffer")
        if next_offset != 0 and (next_offset % 4 != 0 or next_offset < 12 + name_length):
            raise _NotificationParseError("notification record offset is invalid")
        raw_name = buffer[offset + 12 : minimum_end]
        try:
            name = raw_name.decode("utf-16-le", errors="strict")
        except UnicodeDecodeError as error:
            raise _NotificationParseError("notification name is not valid UTF-16") from error
        try:
            normalized = _normalize_relative_path(name)
        except ValueError as error:
            raise _NotificationParseError(str(error)) from error
        notifications.append((action, normalized))
        if next_offset == 0:
            break
        offset = record_end
    return tuple(notifications)


def _normalize_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH_TEXT:
        raise ValueError("notification path is not bounded text")
    if "\x00" in value:
        raise ValueError("notification path contains a NUL")
    candidate = value.replace("/", "\\")
    drive, tail = ntpath.splitdrive(candidate)
    if drive or ntpath.isabs(candidate) or tail.startswith("\\"):
        raise ValueError("notification path must be relative")
    parts = PureWindowsPath(candidate).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("notification path contains an unsafe component")
    normalized = ntpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("..\\"):
        raise ValueError("notification path escapes its monitored root")
    return normalized


def _root_metadata_failure(metadata: FileSystemMetadata) -> FallbackReason | None:
    if not metadata.is_directory or metadata.is_reparse_point or metadata.is_cloud_placeholder:
        return FallbackReason.ROOT_CHANGED
    if metadata.identity is None:
        return FallbackReason.ROOT_IDENTITY_UNAVAILABLE
    return None


def _require_sequence(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_detail(value: str) -> str:
    return value[:_MAX_DETAIL_TEXT]


__all__ = [
    "ChangeAction",
    "ChangeBatch",
    "ChangeHint",
    "DirectoryChangeMonitor",
    "MonitorState",
]
