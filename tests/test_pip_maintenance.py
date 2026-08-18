from __future__ import annotations

import os
import subprocess
from pathlib import Path

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


def test_pip_inventory_is_read_only_and_marks_large_cache_recommended(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, custom_cache = _layout(tmp_path)
    sizes = {default_cache: 600 * 1024**2, custom_cache: 100 * 1024**2}
    monkeypatch.setattr(pip_maintenance, "_directory_bytes", lambda path: sizes[path])

    inventory = inventory_pip_storage(env)
    by_path = {entry.path: entry for entry in inventory.caches}

    assert by_path[default_cache].recommended
    assert not by_path[default_cache].custom
    assert not by_path[custom_cache].recommended
    assert by_path[custom_cache].custom
    assert inventory.total_cache_bytes == 700 * 1024**2
    assert inventory.recommended_bytes == 600 * 1024**2


def test_pip_purge_validates_cache_dir_then_uses_same_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    payload = default_cache / "payload.bin"
    payload.write_bytes(b"x" * 41)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    monkeypatch.setattr(
        pip_maintenance,
        "pip_command_candidates",
        lambda: (("python-test", "-m", "pip"),),
    )
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert check is False
        assert capture_output is True
        assert text is True
        assert os.path.normcase(env["PIP_CACHE_DIR"]) == os.path.normcase(str(default_cache))
        if command[-2:] == ["cache", "dir"]:
            assert timeout == 30
            return subprocess.CompletedProcess(command, 0, stdout=str(default_cache), stderr="")
        assert command[-2:] == ["cache", "purge"]
        assert timeout == 600
        payload.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="Files removed: 1", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = purge_pip_cache(default_cache, env)

    assert calls == [
        ["python-test", "-m", "pip", "cache", "dir"],
        ["python-test", "-m", "pip", "cache", "purge"],
    ]
    assert result.before_bytes == 41
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 41
    assert result.command == ("python-test", "-m", "pip", "cache", "purge")


def test_pip_purge_can_scope_custom_cache_through_vendor_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, custom_cache = _layout(tmp_path)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    monkeypatch.setattr(
        pip_maintenance,
        "pip_command_candidates",
        lambda: (("pip-test",),),
    )

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        assert process_env["PIP_CACHE_DIR"] == str(custom_cache)
        if command[-2:] == ["cache", "dir"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(custom_cache), stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="cleared", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = purge_pip_cache(custom_cache, env)
    assert result.cache_path == custom_cache


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


def test_pip_purge_fails_closed_when_no_command_confirms_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(pip_maintenance, "pip_process_running", lambda: False)
    monkeypatch.setattr(
        pip_maintenance,
        "pip_command_candidates",
        lambda: (("pip-test",),),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["pip-test"], 0, stdout=str(tmp_path / "other-cache"), stderr=""
        ),
    )
    with pytest.raises(RuntimeError, match="确认目标 cache"):
        purge_pip_cache(default_cache, env)
