from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

import devclean.scanner.change_monitor as monitor_module
from devclean.core.incremental import FallbackReason
from devclean.scanner.change_monitor import (
    ChangeAction,
    DirectoryChangeMonitor,
    MonitorState,
)


class _ManualBackend:
    def __init__(self) -> None:
        self.callbacks: monitor_module._BackendCallbacks | None = None
        self.ready = Event()

    def run(self, callbacks: monitor_module._BackendCallbacks) -> None:
        self.callbacks = callbacks
        callbacks.ready()
        self.ready.set()
        callbacks.stop_event.wait(5)

    def request_stop(self) -> None:
        callbacks = self.callbacks
        if callbacks is not None:
            callbacks.stop_event.set()

    def emit(self, action: ChangeAction, relative_path: str) -> bool:
        assert self.callbacks is not None
        return self.callbacks.publish(action, relative_path)

    def fail(self, reason: FallbackReason, detail: str = "fixture failure") -> None:
        assert self.callbacks is not None
        self.callbacks.fail(reason, detail)


def _start_manual_monitor(
    root: Path, *, capacity: int = 8
) -> tuple[DirectoryChangeMonitor, _ManualBackend]:
    backend = _ManualBackend()
    monitor = DirectoryChangeMonitor(
        root,
        max_pending_hints=capacity,
        _backend=backend,
        _platform_supported=True,
    ).start()
    assert monitor.wait_until_ready(2)
    assert backend.ready.wait(2)
    assert monitor.state is MonitorState.ACTIVE
    return monitor, backend


def _notification_record(action: int, name: str, *, has_next: bool) -> bytes:
    encoded = name.encode("utf-16-le")
    raw_length = 12 + len(encoded)
    padded_length = (raw_length + 3) & ~3
    next_offset = padded_length if has_next else 0
    return (
        struct.pack("<III", next_offset, action, len(encoded))
        + encoded
        + b"\x00" * (padded_length - raw_length)
    )


def test_notification_parser_handles_all_material_actions() -> None:
    records = [
        (1, "cache\\new.bin", ChangeAction.ADDED),
        (3, "cache\\new.bin", ChangeAction.MODIFIED),
        (4, "cache\\new.bin", ChangeAction.RENAMED_OLD_NAME),
        (5, "cache\\renamed.bin", ChangeAction.RENAMED_NEW_NAME),
        (2, "cache\\renamed.bin", ChangeAction.REMOVED),
    ]
    payload = b"".join(
        _notification_record(action, name, has_next=index < len(records) - 1)
        for index, (action, name, _) in enumerate(records)
    )

    parsed = monitor_module._parse_notification_buffer(payload, len(payload))

    assert parsed == tuple((expected, name) for _, name, expected in records)


@pytest.mark.parametrize(
    "payload",
    [
        b"short",
        struct.pack("<III", 0, 99, 2) + b"x\x00",
        struct.pack("<III", 0, 1, 3) + b"x\x00x",
        struct.pack("<III", 8, 1, 2) + b"x\x00",
        struct.pack("<III", 0, 1, 6) + b".\x00.\x00\\\x00",
    ],
)
def test_malformed_notifications_are_rejected(payload: bytes) -> None:
    with pytest.raises(monitor_module._NotificationParseError):
        monitor_module._parse_notification_buffer(payload, len(payload))


def test_unsupported_platform_is_explicit_and_requires_full_scan(tmp_path: Path) -> None:
    monitor = DirectoryChangeMonitor(tmp_path, _platform_supported=False).start()

    assert monitor.wait_until_ready(0.1)
    batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
    assert batch.state is MonitorState.UNSUPPORTED
    assert batch.fallback_reason is FallbackReason.UNSUPPORTED_PLATFORM
    assert batch.requires_full_scan is True


def test_change_hints_are_bounded_and_over_capacity_fails_closed(tmp_path: Path) -> None:
    monitor, backend = _start_manual_monitor(tmp_path, capacity=2)
    try:
        assert backend.emit(ChangeAction.ADDED, "one.bin")
        assert backend.emit(ChangeAction.MODIFIED, "one.bin")
        assert not backend.emit(ChangeAction.ADDED, "two.bin")
        batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)

        assert batch.state is MonitorState.FAILED
        assert batch.fallback_reason is FallbackReason.MONITOR_CAPACITY_EXCEEDED
        assert batch.requires_full_scan is True
        assert [hint.sequence for hint in batch.hints] == [1, 2]
    finally:
        monitor.stop()


@pytest.mark.parametrize(
    "reason",
    [
        FallbackReason.MONITOR_BUFFER_OVERFLOW,
        FallbackReason.MONITOR_HANDLE_LOST,
        FallbackReason.MONITOR_IO_ERROR,
        FallbackReason.MALFORMED_NOTIFICATION,
    ],
)
def test_backend_failure_reasons_require_full_scan(tmp_path: Path, reason: FallbackReason) -> None:
    monitor, backend = _start_manual_monitor(tmp_path)
    try:
        backend.fail(reason)
        batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
        assert batch.state is MonitorState.FAILED
        assert batch.fallback_reason is reason
        assert batch.requires_full_scan is True
    finally:
        monitor.stop()


def test_acknowledgement_is_explicit_and_sequence_gaps_fail_closed(tmp_path: Path) -> None:
    monitor, backend = _start_manual_monitor(tmp_path)
    try:
        assert backend.emit(ChangeAction.ADDED, "one.bin")
        assert backend.emit(ChangeAction.ADDED, "two.bin")
        assert monitor.acknowledge(session_token=monitor.session_token, through_sequence=1) is None

        stale = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
        current = monitor.read_changes(session_token=monitor.session_token, after_sequence=1)

        assert stale.fallback_reason is FallbackReason.SESSION_SEQUENCE_GAP
        assert stale.requires_full_scan is True
        assert [(hint.sequence, hint.relative_path) for hint in current.hints] == [(2, "two.bin")]
        assert current.requires_full_scan is False
    finally:
        monitor.stop()


def test_stopped_and_restarted_sessions_cannot_reuse_tokens(tmp_path: Path) -> None:
    first, _ = _start_manual_monitor(tmp_path)
    old_token = first.session_token
    first.stop()
    stopped = first.read_changes(session_token=old_token, after_sequence=0)
    assert stopped.fallback_reason is FallbackReason.MONITOR_STOPPED
    assert stopped.requires_full_scan is True

    second, _ = _start_manual_monitor(tmp_path)
    try:
        mismatch = second.read_changes(session_token=old_token, after_sequence=0)
        assert second.session_token != old_token
        assert mismatch.fallback_reason is FallbackReason.SESSION_TOKEN_MISMATCH
        assert mismatch.requires_full_scan is True
    finally:
        second.stop()


def test_notification_path_is_never_allowed_to_escape_root(tmp_path: Path) -> None:
    monitor, backend = _start_manual_monitor(tmp_path)
    try:
        assert not backend.emit(ChangeAction.ADDED, "..\\outside.bin")
        batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
        assert batch.fallback_reason is FallbackReason.MALFORMED_NOTIFICATION
        assert batch.requires_full_scan is True
        assert batch.hints == ()
    finally:
        monitor.stop()


def test_root_identity_change_invalidates_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monitor, _ = _start_manual_monitor(tmp_path)
    original_reader = monitor_module.read_file_metadata

    def changed_reader(path: str) -> monitor_module.FileSystemMetadata:
        metadata = original_reader(path)
        return replace(metadata, file_id=(metadata.file_id or "0") + "changed")

    monkeypatch.setattr(monitor_module, "read_file_metadata", changed_reader)
    try:
        batch = monitor.read_changes(session_token=monitor.session_token, after_sequence=0)
        assert batch.fallback_reason is FallbackReason.ROOT_CHANGED
        assert batch.requires_full_scan is True
    finally:
        monitor.stop()
