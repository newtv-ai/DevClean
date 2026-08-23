from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import devclean.core.pip_maintenance as pip_maintenance
from devclean.core.pip_maintenance import inventory_pip_storage, purge_pip_cache


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    default_cache = tmp_path / "Local" / "pip" / "Cache"
    custom_cache = tmp_path / "custom-pip-cache"
    default_cache.mkdir(parents=True)
    custom_cache.mkdir(parents=True)
    env = {
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "PIP_CACHE_DIR": str(custom_cache),
    }
    return env, default_cache, custom_cache


def _bind_fake_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, ...]:
    executable = tmp_path / "python-test.exe"
    executable.write_bytes(b"fake-python")
    prefix = (str(executable.resolve()), "-m", "pip")
    monkeypatch.setattr(
        pip_maintenance,
        "pip_command_candidates",
        lambda: ((str(executable), "-m", "pip"),),
    )
    return prefix


def _probe_or_none(
    command: list[str],
    kwargs: dict[str, Any],
    *,
    cache_path: Path,
    version: str = "pip 26.0 from C:\\Python\\Lib\\site-packages\\pip (python 3.13)",
) -> subprocess.CompletedProcess[str] | None:
    process_env = kwargs["env"]
    assert isinstance(process_env, dict)
    assert os.path.normcase(process_env["PIP_CACHE_DIR"]) == os.path.normcase(
        str(cache_path.resolve())
    )
    if command[-1:] == ["--version"]:
        return subprocess.CompletedProcess(command, 0, stdout=f"{version}\n", stderr="")
    if command[-2:] == ["cache", "dir"]:
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{cache_path.resolve()}\n",
            stderr="",
        )
    return None


def test_pip_inventory_keeps_root_occupancy_separate_from_reclaim_recommendation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)
    sizes = {default_cache: 600 * 1024**2, custom_cache: 100 * 1024**2}
    monkeypatch.setattr(pip_maintenance, "_directory_bytes", lambda path: sizes[path])

    inventory = inventory_pip_storage(env)
    by_path = {entry.path: entry for entry in inventory.caches}

    assert not by_path[default_cache].recommended
    assert not by_path[default_cache].custom
    assert not by_path[custom_cache].recommended
    assert by_path[custom_cache].custom
    assert inventory.total_cache_bytes == 700 * 1024**2
    assert inventory.recommended_bytes == 0


def test_pip_purge_binds_command_and_cache_then_revalidates_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    prefix = _bind_fake_pip(tmp_path, monkeypatch)
    payload = default_cache / "payload.bin"
    payload.write_bytes(b"x" * 41)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        assert command == [*prefix, "cache", "purge"]
        assert kwargs["timeout"] == 600
        payload.unlink()
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Files removed: 1",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = purge_pip_cache(default_cache, env)

    assert calls == [
        [*prefix, "--version"],
        [*prefix, "cache", "dir"],
        [*prefix, "--version"],
        [*prefix, "cache", "dir"],
        [*prefix, "cache", "purge"],
    ]
    assert result.before_bytes == 41
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 41
    assert result.command == (*prefix, "cache", "purge")


def test_pip_purge_can_scope_custom_cache_through_vendor_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, custom_cache = _layout(tmp_path)
    prefix = _bind_fake_pip(tmp_path, monkeypatch)
    payload = custom_cache / "pip-owned-item"
    unrelated = custom_cache / "user-note.txt"
    payload.write_bytes(b"cache")
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probe = _probe_or_none(command, kwargs, cache_path=custom_cache)
        if probe is not None:
            return probe
        assert command == [*prefix, "cache", "purge"]
        payload.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="cleared", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = purge_pip_cache(custom_cache, env)

    assert result.cache_path == custom_cache.resolve()
    assert unrelated.exists()
    assert result.after_bytes == unrelated.stat().st_size
    assert result.reclaimed_bytes == len(b"cache")


def test_pip_purge_refuses_unrecognized_root(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    arbitrary = tmp_path / "not-pip"
    arbitrary.mkdir()
    with pytest.raises(ValueError, match="已审计"):
        purge_pip_cache(arbitrary, env)


def test_pip_purge_refuses_while_pip_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: True)
    with pytest.raises(RuntimeError, match="pip 正在运行"):
        purge_pip_cache(default_cache, env)


def test_pip_purge_rechecks_process_immediately_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    _bind_fake_pip(tmp_path, monkeypatch)
    states = iter((False, True))
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: next(states))
    purge_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal purge_called
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        purge_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pip 正在运行"):
        purge_pip_cache(default_cache, env)
    assert not purge_called


def test_pip_purge_fails_closed_when_no_command_confirms_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    _bind_fake_pip(tmp_path, monkeypatch)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    wrong_cache = (tmp_path / "other-cache").resolve()

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        assert os.path.normcase(process_env["PIP_CACHE_DIR"]) == os.path.normcase(
            str(default_cache.resolve())
        )
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="pip 26.0 from C:\\Python\\Lib\\site-packages\\pip (python 3.13)\n",
                stderr="",
            )
        assert command[-2:] == ["cache", "dir"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{wrong_cache}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="确认目标 cache"):
        purge_pip_cache(default_cache, env)


def test_pip_purge_revalidates_selected_pip_environment_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    _bind_fake_pip(tmp_path, monkeypatch)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    version_calls = 0
    purge_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal version_calls, purge_called
        if command[-1:] == ["--version"]:
            version_calls += 1
            version = "pip 26.0" if version_calls == 1 else "pip 27.0"
            return subprocess.CompletedProcess(command, 0, stdout=version, stderr="")
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        purge_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pip 环境身份"):
        purge_pip_cache(default_cache, env)
    assert not purge_called


def test_pip_purge_revalidates_cli_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    _bind_fake_pip(tmp_path, monkeypatch)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    real_identity = pip_maintenance._path_identity
    command_identity_calls = 0
    purge_called = False

    def identity(
        path: Path,
        *,
        expect_directory: bool,
        label: str,
    ) -> pip_maintenance.PipPathIdentity:
        nonlocal command_identity_calls
        current = real_identity(path, expect_directory=expect_directory, label=label)
        if label == "pip 命令":
            command_identity_calls += 1
            if command_identity_calls >= 3:
                return replace(
                    current,
                    last_write_time_ns=(current.last_write_time_ns or 0) + 1,
                )
        return current

    monkeypatch.setattr(pip_maintenance, "_path_identity", identity)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal purge_called
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        purge_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pip 命令 身份发生变化"):
        purge_pip_cache(default_cache, env)
    assert not purge_called


def test_pip_purge_revalidates_cache_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    _bind_fake_pip(tmp_path, monkeypatch)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    real_identity = pip_maintenance._path_identity
    cache_identity_calls = 0
    purge_called = False

    def identity(
        path: Path,
        *,
        expect_directory: bool,
        label: str,
    ) -> pip_maintenance.PipPathIdentity:
        nonlocal cache_identity_calls
        current = real_identity(path, expect_directory=expect_directory, label=label)
        if label == "pip cache":
            cache_identity_calls += 1
            if cache_identity_calls >= 2:
                return replace(current, file_id=current.file_id + "-changed")
        return current

    monkeypatch.setattr(pip_maintenance, "_path_identity", identity)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal purge_called
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        purge_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="pip cache 身份发生变化"):
        purge_pip_cache(default_cache, env)
    assert not purge_called


def test_pip_purge_surfaces_vendor_failure_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    prefix = _bind_fake_pip(tmp_path, monkeypatch)
    payload = default_cache / "keep.bin"
    payload.write_bytes(b"keep")
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probe = _probe_or_none(command, kwargs, cache_path=default_cache)
        if probe is not None:
            return probe
        assert command == [*prefix, "cache", "purge"]
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="cache busy")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache busy"):
        purge_pip_cache(default_cache, env)
    assert payload.exists()
