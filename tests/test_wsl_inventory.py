from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.wsl_inventory as wsl_inventory
from devclean.core.wsl_inventory import inspect_wsl


def _completed(
    args: Sequence[str],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        list(args),
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def test_wsl_inventory_uses_only_read_only_vendor_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, text, timeout, env
        cmd = list(command)
        seen.append(cmd)
        if cmd == ["wsl-test", "--list", "--quiet"]:
            return _completed(command, stdout=_utf16("Ubuntu\r\nDebian\r\n"))
        if cmd == ["wsl-test", "--list", "--running", "--quiet"]:
            return _completed(command, stdout=_utf16("Ubuntu\r\n"))
        if cmd == ["wsl-test", "--version"]:
            return _completed(command, stdout=_utf16("WSL version: 2.6.0\r\n"))
        if cmd == ["wsl-test", "--status"]:
            return _completed(command, stdout=_utf16("Default Version: 2\r\n"))
        raise AssertionError(cmd)

    monkeypatch.setattr("devclean.core.wsl_inventory.subprocess.run", fake_run)

    inventory = inspect_wsl({"DEVCLEAN_WSL_EXE": "wsl-test"})

    assert [(item.name, item.running) for item in inventory.distributions] == [
        ("Ubuntu", True),
        ("Debian", False),
    ]
    assert "2.6.0" in inventory.version_text
    assert "Default Version" in inventory.status_text
    assert all("--unregister" not in command for command in seen)
    assert all("--terminate" not in command for command in seen)
    assert all("--shutdown" not in command for command in seen)
    assert all("--manage" not in command for command in seen)


def test_wsl_inventory_handles_utf8_output_and_deduplicates_casefolded_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, text, timeout, env
        cmd = list(command)
        if cmd == ["wsl-test", "--list", "--quiet"]:
            return _completed(command, stdout=b"* Ubuntu\nubuntu\nDebian\n")
        if cmd == ["wsl-test", "--list", "--running", "--quiet"]:
            return _completed(command, stdout=b"ubuntu\n")
        if cmd in (["wsl-test", "--version"], ["wsl-test", "--status"]):
            return _completed(command, returncode=1)
        raise AssertionError(cmd)

    monkeypatch.setattr("devclean.core.wsl_inventory.subprocess.run", fake_run)

    inventory = inspect_wsl({"DEVCLEAN_WSL_EXE": "wsl-test"})

    assert [(item.name, item.running) for item in inventory.distributions] == [
        ("Ubuntu", True),
        ("Debian", False),
    ]
    assert inventory.version_text == ""
    assert inventory.status_text == ""


def test_wsl_inventory_fails_closed_if_running_state_cannot_be_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, text, timeout, env
        cmd = list(command)
        if cmd == ["wsl-test", "--list", "--quiet"]:
            return _completed(command, stdout=_utf16("Ubuntu\r\n"))
        if cmd == ["wsl-test", "--list", "--running", "--quiet"]:
            return _completed(command, stderr=_utf16("WSL unavailable"), returncode=1)
        raise AssertionError(cmd)

    monkeypatch.setattr("devclean.core.wsl_inventory.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="running distributions"):
        inspect_wsl({"DEVCLEAN_WSL_EXE": "wsl-test"})


def test_wsl_list_failure_surfaces_vendor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, text, timeout, env
        return _completed(
            command,
            stderr=_utf16("The Windows Subsystem for Linux is not installed."),
            returncode=1,
        )

    monkeypatch.setattr("devclean.core.wsl_inventory.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="not installed"):
        inspect_wsl({"DEVCLEAN_WSL_EXE": "wsl-test"})


def test_configured_wsl_executable_is_respected() -> None:
    assert (
        wsl_inventory.wsl_executable({"DEVCLEAN_WSL_EXE": "C:/Tools/wsl.exe"}) == "C:/Tools/wsl.exe"
    )
