from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from threading import Event, Timer

import pytest

import devclean.platform.windows.process as process_module
from devclean.platform.windows.process import (
    ProcessLimits,
    ProcessTermination,
    run_bounded_process,
)


def _environment() -> dict[str, str]:
    keys = ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP")
    return {key: os.environ[key] for key in keys if key in os.environ}


def _tree_script(pid_file: Path, gate_file: Path, tail: str) -> str:
    child_script = "import time; time.sleep(30)"
    return "\n".join(
        (
            "import pathlib, subprocess, sys, time",
            f"gate = pathlib.Path({str(gate_file)!r})",
            "while not gate.exists(): time.sleep(0.01)",
            f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}])",
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid), encoding='ascii')",
            "print('tree-ready', flush=True)",
            tail,
        )
    )


def _open_gate_later(gate_file: Path) -> Timer:
    timer = Timer(0.2, lambda: gate_file.write_text("go", encoding="ascii"))
    timer.start()
    return timer


def _windows_pid_is_active(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        close_handle(handle)


def _assert_windows_pid_stopped(pid_file: Path) -> None:
    assert pid_file.is_file(), "child process did not publish its PID"
    pid = int(pid_file.read_text(encoding="ascii"))
    deadline = time.monotonic() + 3
    while _windows_pid_is_active(pid) and time.monotonic() < deadline:
        Event().wait(0.02)
    assert not _windows_pid_is_active(pid), f"descendant PID {pid} survived Job cleanup"


def test_bounded_process_captures_stdout_and_stderr(tmp_path: Path) -> None:
    result = run_bounded_process(
        (
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ),
        environment=_environment(),
        cwd=tmp_path,
    )

    assert result.succeeded
    assert result.termination is ProcessTermination.EXITED
    assert result.stdout.strip() == b"out"
    assert result.stderr.strip() == b"err"


def test_bounded_process_times_out_and_returns_partial_evidence(tmp_path: Path) -> None:
    result = run_bounded_process(
        (
            sys.executable,
            "-c",
            "import sys,time; print('started', flush=True); time.sleep(10)",
        ),
        environment=_environment(),
        cwd=tmp_path,
        limits=ProcessLimits(timeout_seconds=0.1),
    )

    assert result.timed_out
    assert result.stdout.strip() == b"started"
    assert result.duration_ms < 5000


def test_bounded_process_stops_at_output_limit(tmp_path: Path) -> None:
    result = run_bounded_process(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"),
        environment=_environment(),
        cwd=tmp_path,
        limits=ProcessLimits(max_stdout_bytes=1024, timeout_seconds=5),
    )

    assert result.output_limit_exceeded
    assert len(result.stdout) == 1024
    assert result.duration_ms < 5000


def test_bounded_process_rejects_relative_executable(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute executable"):
        run_bounded_process(
            ("python", "--version"),
            environment=_environment(),
            cwd=tmp_path,
        )


@pytest.mark.parametrize("name", ("query.cmd", "query.bat", "query.CMD", "query"))
def test_bounded_process_rejects_non_executable_extensions(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"\.exe"):
        run_bounded_process(
            (str(tmp_path / name), "--version"),
            environment=_environment(),
            cwd=tmp_path,
        )


def test_process_limits_reject_invalid_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        ProcessLimits(timeout_seconds=0)
    with pytest.raises(ValueError, match="non-negative"):
        ProcessLimits(max_stderr_bytes=-1)


def test_bounded_process_kills_child_when_caller_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []
    original_popen = process_module.subprocess.Popen

    def recording_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    def interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(process_module.time, "sleep", interrupt)

    with pytest.raises(KeyboardInterrupt):
        run_bounded_process(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            environment=_environment(),
            cwd=tmp_path,
            limits=ProcessLimits(timeout_seconds=5),
        )

    assert created
    assert created[0].poll() is not None


def test_job_creation_failure_never_launches_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_creation():
        raise OSError("job creation failed")

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("Popen must not run without containment")

    monkeypatch.setattr(process_module, "_create_windows_job", fail_creation)
    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)

    with pytest.raises(OSError, match="job creation failed"):
        run_bounded_process(
            (sys.executable, "-c", "print('must not run')"),
            environment=_environment(),
            cwd=tmp_path,
        )


def test_job_assignment_failure_kills_direct_child_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = []
    original_popen = process_module.subprocess.Popen

    class FailingJob:
        assigned = False
        closed = False

        def assign(self, process) -> None:
            raise OSError("assignment failed")

        def close(self) -> None:
            self.closed = True

    fake_job = FailingJob()

    def recording_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(process_module, "_create_windows_job", lambda: fake_job)
    monkeypatch.setattr(process_module.subprocess, "Popen", recording_popen)

    with pytest.raises(RuntimeError, match="assignment failed"):
        run_bounded_process(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            environment=_environment(),
            cwd=tmp_path,
        )

    assert fake_job.closed
    assert created and created[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_kills_descendant_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "timeout-child.pid"
    gate_file = tmp_path / "timeout.go"
    timer = _open_gate_later(gate_file)
    try:
        result = run_bounded_process(
            (
                sys.executable,
                "-c",
                _tree_script(pid_file, gate_file, "time.sleep(30)"),
            ),
            environment=_environment(),
            cwd=tmp_path,
            limits=ProcessLimits(timeout_seconds=1),
        )
    finally:
        timer.cancel()
        timer.join()

    assert result.timed_out
    assert b"tree-ready" in result.stdout
    _assert_windows_pid_stopped(pid_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_kills_descendant_on_output_limit(tmp_path: Path) -> None:
    pid_file = tmp_path / "output-child.pid"
    gate_file = tmp_path / "output.go"
    timer = _open_gate_later(gate_file)
    try:
        result = run_bounded_process(
            (
                sys.executable,
                "-c",
                _tree_script(
                    pid_file,
                    gate_file,
                    "sys.stdout.write('x' * 1000000); sys.stdout.flush(); time.sleep(30)",
                ),
            ),
            environment=_environment(),
            cwd=tmp_path,
            limits=ProcessLimits(max_stdout_bytes=1024, timeout_seconds=5),
        )
    finally:
        timer.cancel()
        timer.join()

    assert result.output_limit_exceeded
    assert len(result.stdout) == 1024
    _assert_windows_pid_stopped(pid_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_kills_residual_descendant_after_normal_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "normal-child.pid"
    gate_file = tmp_path / "normal.go"
    timer = _open_gate_later(gate_file)
    try:
        result = run_bounded_process(
            (
                sys.executable,
                "-c",
                _tree_script(pid_file, gate_file, "pass"),
            ),
            environment=_environment(),
            cwd=tmp_path,
        )
    finally:
        timer.cancel()
        timer.join()

    assert result.succeeded
    assert b"tree-ready" in result.stdout
    _assert_windows_pid_stopped(pid_file)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration test")
def test_windows_job_kills_descendant_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "interrupt-child.pid"
    gate_file = tmp_path / "interrupt.go"
    timer = _open_gate_later(gate_file)
    original_sleep = process_module.time.sleep

    def interrupt_after_child_exists(_seconds: float) -> None:
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            Event().wait(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(process_module.time, "sleep", interrupt_after_child_exists)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_bounded_process(
                (
                    sys.executable,
                    "-c",
                    _tree_script(pid_file, gate_file, "time.sleep(30)"),
                ),
                environment=_environment(),
                cwd=tmp_path,
                limits=ProcessLimits(timeout_seconds=5),
            )
    finally:
        monkeypatch.setattr(process_module.time, "sleep", original_sleep)
        timer.cancel()
        timer.join()

    _assert_windows_pid_stopped(pid_file)
