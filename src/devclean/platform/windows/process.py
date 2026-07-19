"""Bounded subprocess primitives for observational vendor queries.

This module does not decide which command is safe.  It enforces process-level
mechanics after an audited adapter has supplied an absolute executable and a
minimal environment: no shell, no stdin, bounded output, timeout, hidden
Windows console, and process-tree containment.  Windows queries are assigned
to a per-invocation Job Object configured with ``KILL_ON_JOB_CLOSE``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event, Thread
from typing import IO, Final

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_READ_CHUNK: Final = 64 * 1024
_POLL_SECONDS: Final = 0.01
_READER_JOIN_SECONDS: Final = 2.0
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_TERMINATE_EXIT_CODE: Final = 1


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _JobObjectBasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


@dataclass(slots=True)
class _WindowsJob:
    handle: int | None
    assigned: bool = False

    @classmethod
    def create(cls) -> _WindowsJob:
        if os.name != "nt":
            raise RuntimeError("Windows Job Objects are unavailable on this platform")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = kernel32.CreateJobObjectW
        create_job.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        create_job.restype = wintypes.HANDLE
        set_information = kernel32.SetInformationJobObject
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        raw_handle = create_job(None, None)
        if not raw_handle:
            raise _windows_api_error("CreateJobObjectW")
        handle = int(raw_handle)
        limits = _JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not set_information(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = _windows_api_error("SetInformationJobObject")
            close_handle(handle)
            raise error
        return cls(handle)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if self.handle is None:
            raise RuntimeError("cannot assign a process to a closed Job Object")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise RuntimeError("Popen did not expose a Windows process handle")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        assign_process = kernel32.AssignProcessToJobObject
        assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        assign_process.restype = wintypes.BOOL
        if not assign_process(self.handle, int(process_handle)):
            raise _windows_api_error("AssignProcessToJobObject")
        self.assigned = True

    def terminate(self) -> None:
        if not self.assigned or self.handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        terminate_job = kernel32.TerminateJobObject
        terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate_job.restype = wintypes.BOOL
        if not terminate_job(self.handle, _JOB_TERMINATE_EXIT_CODE):
            raise _windows_api_error("TerminateJobObject")

    def close(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        # TerminateJobObject is called before this for assigned jobs.  The
        # KILL_ON_JOB_CLOSE flag remains a kernel-enforced second layer.
        close_handle(handle)


class ProcessTermination(StrEnum):
    EXITED = "EXITED"
    TIMED_OUT = "TIMED_OUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    duration_ms: int
    termination: ProcessTermination

    @property
    def succeeded(self) -> bool:
        return self.termination is ProcessTermination.EXITED and self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.termination is ProcessTermination.TIMED_OUT

    @property
    def output_limit_exceeded(self) -> bool:
        return self.termination is ProcessTermination.OUTPUT_LIMIT


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    timeout_seconds: float = 10.0
    max_stdout_bytes: int = 2 * 1024 * 1024
    max_stderr_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_stdout_bytes < 0 or self.max_stderr_bytes < 0:
            raise ValueError("output limits must be non-negative")


@dataclass(slots=True)
class _Capture:
    limit: int
    data: bytearray
    exceeded: Event


def run_bounded_process(
    argv: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    limits: ProcessLimits | None = None,
) -> BoundedProcessResult:
    """Run an already-approved observational command with hard resource bounds."""

    normalized = _validate_invocation(argv, cwd)
    active_limits = limits or ProcessLimits()
    output_exceeded = Event()
    stdout_capture = _Capture(active_limits.max_stdout_bytes, bytearray(), output_exceeded)
    stderr_capture = _Capture(active_limits.max_stderr_bytes, bytearray(), output_exceeded)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP

    started = time.monotonic()
    job = _create_windows_job()
    try:
        process = subprocess.Popen(
            normalized,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(environment),
            shell=False,
            close_fds=True,
            creationflags=creation_flags,
        )
        try:
            if job is not None:
                # Popen does not expose a primary-thread handle, so CREATE_SUSPENDED
                # cannot be resumed safely here.  Assignment happens immediately after
                # creation; see docs/adapter-support.md for the residual launch window.
                job.assign(process)
        except (OSError, RuntimeError) as error:
            _fallback_terminate_process_tree(process)
            _wait_and_close_unread_pipes(process)
            raise RuntimeError(
                "Windows Job Object assignment failed; query execution was rejected"
            ) from error

        if process.stdout is None or process.stderr is None:
            _terminate_process_tree(process, job)
            raise RuntimeError("subprocess pipes were not created")

        stdout_thread = Thread(
            target=_capture_stream,
            args=(process.stdout, stdout_capture),
            name="DevClean-stdout-reader",
            daemon=True,
        )
        stderr_thread = Thread(
            target=_capture_stream,
            args=(process.stderr, stderr_capture),
            name="DevClean-stderr-reader",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            termination = ProcessTermination.EXITED
            deadline = started + active_limits.timeout_seconds
            while process.poll() is None:
                if output_exceeded.is_set():
                    termination = ProcessTermination.OUTPUT_LIMIT
                    break
                if time.monotonic() >= deadline:
                    termination = ProcessTermination.TIMED_OUT
                    break
                time.sleep(_POLL_SECONDS)

            # On Windows this always terminates the whole assigned Job, including
            # descendants left behind after an otherwise normal direct-child exit.
            _terminate_process_tree(process, job)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_READER_JOIN_SECONDS)
            stdout_thread.join(timeout=_READER_JOIN_SECONDS)
            stderr_thread.join(timeout=_READER_JOIN_SECONDS)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                _close_pipe(process.stdout)
                _close_pipe(process.stderr)
                stdout_thread.join(timeout=_READER_JOIN_SECONDS)
                stderr_thread.join(timeout=_READER_JOIN_SECONDS)
        except BaseException as error:
            try:
                _terminate_process_tree(process, job)
            except (OSError, RuntimeError) as cleanup_error:
                error.add_note(f"process-tree cleanup also failed: {cleanup_error}")
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_READER_JOIN_SECONDS)
            _close_pipe(process.stdout)
            _close_pipe(process.stderr)
            stdout_thread.join(timeout=_READER_JOIN_SECONDS)
            stderr_thread.join(timeout=_READER_JOIN_SECONDS)
            raise
        _close_pipe(process.stdout)
        _close_pipe(process.stderr)

        if output_exceeded.is_set() and termination is ProcessTermination.EXITED:
            termination = ProcessTermination.OUTPUT_LIMIT

        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return BoundedProcessResult(
            argv=normalized,
            returncode=process.poll(),
            stdout=bytes(stdout_capture.data),
            stderr=bytes(stderr_capture.data),
            duration_ms=duration_ms,
            termination=termination,
        )
    finally:
        if job is not None:
            job.close()


def _validate_invocation(argv: Sequence[str], cwd: Path) -> tuple[str, ...]:
    if not argv:
        raise ValueError("argv must not be empty")
    normalized = tuple(os.fspath(item) for item in argv)
    if any(not item or "\x00" in item for item in normalized):
        raise ValueError("argv entries must be non-empty and contain no NUL")
    executable = validate_executable_path(Path(normalized[0]))
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    return (str(executable), *normalized[1:])


def validate_executable_path(executable: Path) -> Path:
    """Return one ordinary, resolved local ``.exe`` suitable for a query launch.

    Windows may dispatch batch files through ``cmd.exe`` even when callers pass
    ``shell=False``.  Restricting the process boundary to regular ``.exe`` files
    keeps command arguments out of that implicit shell path.
    """

    candidate = Path(executable)
    if not candidate.is_absolute():
        raise ValueError("query executable must be an absolute executable path")
    if candidate.suffix.casefold() != ".exe":
        raise ValueError("query executable must be a .exe file")
    if not is_local_fixed_path(candidate):
        raise ValueError(
            "query executable must use a fixed local path without reparse ancestors"
        )
    metadata = read_file_metadata(candidate)
    if (
        metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or not candidate.is_file()
    ):
        raise ValueError("query executable must be an ordinary non-cloud file")
    resolved = candidate.resolve(strict=True)
    if resolved.suffix.casefold() != ".exe" or not is_local_fixed_path(resolved):
        raise ValueError("query executable must resolve to a fixed local .exe file")
    resolved_metadata = read_file_metadata(resolved)
    if (
        resolved_metadata.is_directory
        or resolved_metadata.is_reparse_point
        or resolved_metadata.is_cloud_placeholder
        or not resolved.is_file()
    ):
        raise ValueError("query executable must resolve to an ordinary non-cloud file")
    return resolved


def _capture_stream(stream: IO[bytes], capture: _Capture) -> None:
    try:
        while chunk := stream.read(_READ_CHUNK):
            remaining = capture.limit - len(capture.data)
            if remaining > 0:
                capture.data.extend(chunk[:remaining])
            if len(chunk) > max(0, remaining):
                capture.exceeded.set()
    except (OSError, ValueError):
        # Closing a pipe is the final bounded fallback when a descendant keeps
        # an inherited handle open after the direct child has exited.
        return


def _create_windows_job() -> _WindowsJob | None:
    """Create and configure containment before launching any Windows child."""

    if os.name != "nt":
        return None
    # Creation/configuration failure propagates before Popen: an uncontained
    # vendor query is never accepted as a degraded mode.
    return _WindowsJob.create()


def _windows_api_error(operation: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{operation} failed: {ctypes.FormatError(code)}")


def _terminate_process_tree(
    process: subprocess.Popen[bytes], job: _WindowsJob | None
) -> None:
    if job is not None and job.assigned:
        try:
            job.terminate()
            return
        except OSError as error:
            # Job termination is the primary guarantee.  taskkill is retained
            # only as an emergency attempt before the operation fails closed.
            _fallback_terminate_process_tree(process)
            raise RuntimeError("Windows Job Object termination failed") from error
    _fallback_terminate_process_tree(process)


def _fallback_terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        if taskkill.is_file():
            creation_flags = subprocess.CREATE_NO_WINDOW
            with suppress(OSError, subprocess.TimeoutExpired):
                subprocess.run(
                    (str(taskkill), "/PID", str(process.pid), "/T", "/F"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=2.0,
                    shell=False,
                    creationflags=creation_flags,
                )
    if process.poll() is None:
        with suppress(OSError):
            process.kill()


def _wait_and_close_unread_pipes(process: subprocess.Popen[bytes]) -> None:
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=_READER_JOIN_SECONDS)
    if process.stdout is not None:
        _close_pipe(process.stdout)
    if process.stderr is not None:
        _close_pipe(process.stderr)


def _close_pipe(stream: IO[bytes]) -> None:
    with suppress(OSError):
        stream.close()


__all__ = [
    "BoundedProcessResult",
    "ProcessLimits",
    "ProcessTermination",
    "run_bounded_process",
    "validate_executable_path",
]
