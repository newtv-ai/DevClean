from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import devclean.core.conan_maintenance as conan_maintenance
from devclean.core.conan_maintenance import clean_conan_cache, inventory_conan_storage


def _env(home: Path) -> dict[str, str]:
    return {
        "USERPROFILE": str(home.parent),
        "CONAN_HOME": str(home),
        "DEVCLEAN_CONAN_EXE": "conan-test",
    }


def _completed(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_inventory_uses_conan2_reported_home_and_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan2"
    home.mkdir()
    payload = home / "p" / "abc" / "p"
    payload.mkdir(parents=True)
    package = payload / "lib.a"
    package.write_bytes(b"x" * 41)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[-1] == "--version":
            return _completed(command, "Conan version 2.31.1\n")
        if command[-2:] == ["config", "home"]:
            return _completed(command, f"{home}\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    inventory = inventory_conan_storage(_env(home))

    assert inventory.home == home
    assert inventory.exists
    assert inventory.logical_bytes == 41
    assert inventory.version == "Conan version 2.31.1"
    assert not inventory.recommended
    assert package.exists()
    assert calls == [
        ["conan-test", "--version"],
        ["conan-test", "config", "home"],
    ]


def test_clean_conan_cache_delegates_only_to_vendor_noncritical_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan2"
    generated = home / "p" / "abc" / "b"
    package = home / "p" / "abc" / "p"
    generated.mkdir(parents=True)
    package.mkdir(parents=True)
    build_file = generated / "object.obj"
    package_file = package / "library.lib"
    build_file.write_bytes(b"b" * 53)
    package_file.write_bytes(b"p" * 17)
    calls: list[list[str]] = []

    monkeypatch.setattr(conan_maintenance, "clear_conan_process_cache", lambda: None)
    monkeypatch.setattr(conan_maintenance, "conan_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[-1] == "--version":
            return _completed(command, "Conan version 2.31.1\n")
        if command[-2:] == ["config", "home"]:
            return _completed(command, f"{home}\n")
        if command[1:4] == ["cache", "clean", "*"]:
            build_file.unlink()
            return _completed(command, "Cache cleaned\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_conan_cache(home, _env(home))

    assert result.home == home
    assert result.before_bytes == 70
    assert result.after_bytes == 17
    assert result.reclaimed_bytes == 53
    assert result.command[:4] == ("conan-test", "cache", "clean", "*")
    assert result.command[-2:] == ("-cc", "core:non_interactive=True")
    assert result.output == "Cache cleaned"
    assert package_file.exists()
    assert not build_file.exists()
    assert calls[-1][1:4] == ["cache", "clean", "*"]


def test_clean_refuses_path_that_conan_does_not_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan2"
    home.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[-1] == "--version":
            return _completed(command, "Conan version 2.31.1\n")
        if command[-2:] == ["config", "home"]:
            return _completed(command, f"{home}\n")
        raise AssertionError("vendor clean must not run")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(conan_maintenance, "clear_conan_process_cache", lambda: None)
    monkeypatch.setattr(conan_maintenance, "conan_process_running", lambda: False)

    with pytest.raises(ValueError, match="不是当前 Conan 2 home"):
        clean_conan_cache(other, _env(home))


def test_clean_refuses_while_conan_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan2"
    home.mkdir()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[-1] == "--version":
            return _completed(command, "Conan version 2.31.1\n")
        if command[-2:] == ["config", "home"]:
            return _completed(command, f"{home}\n")
        raise AssertionError("vendor clean must not run")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(conan_maintenance, "clear_conan_process_cache", lambda: None)
    monkeypatch.setattr(conan_maintenance, "conan_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="Conan 正在运行"):
        clean_conan_cache(home, _env(home))


def test_conan1_is_rejected_before_cache_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command == ["conan-test", "--version"]
        return _completed(command, "Conan version 1.66.0\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="需要 Conan 2"):
        inventory_conan_storage(_env(home))


def test_large_conan_home_is_recommended_as_a_benefit_heuristic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".conan2"
    home.mkdir()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if command[-1] == "--version":
            return _completed(command, "Conan version 2.31.1\n")
        if command[-2:] == ["config", "home"]:
            return _completed(command, f"{home}\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(conan_maintenance, "_directory_bytes", lambda path: 2 * 1024**3)

    inventory = inventory_conan_storage(_env(home))

    assert inventory.recommended
