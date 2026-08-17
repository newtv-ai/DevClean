from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.uv_maintenance as uv_maintenance
from devclean.core.uv_maintenance import inventory_uv_storage, prune_uv_cache


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "PROGRAMDATA": str(tmp_path / "ProgramData"),
        "UV_CACHE_DIR": str(cache),
    }
    return env, cache


def test_uv_inventory_is_read_only_and_sums_cache_bytes(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    (cache / "one.bin").write_bytes(b"a" * 17)
    nested = cache / "nested"
    nested.mkdir()
    (nested / "two.bin").write_bytes(b"b" * 23)

    inventory = inventory_uv_storage(env)

    assert len(inventory.caches) == 1
    assert inventory.caches[0].path == cache
    assert inventory.caches[0].exists
    assert inventory.caches[0].logical_bytes == 40
    assert inventory.total_cache_bytes == 40
    assert (cache / "one.bin").exists()
    assert (nested / "two.bin").exists()


def test_uv_prune_uses_vendor_command_for_exact_audited_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    old = cache / "old.bin"
    keep = cache / "keep.bin"
    old.write_bytes(b"x" * 31)
    keep.write_bytes(b"y" * 11)

    monkeypatch.setattr(uv_maintenance, "uv_process_running", lambda: False)

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["cache", "prune"]
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout == 600
        assert os.path.normcase(env["UV_CACHE_DIR"]) == os.path.normcase(str(cache))
        old.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="pruned", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = prune_uv_cache(cache, env)

    assert result.cache_path == cache
    assert result.before_bytes == 42
    assert result.after_bytes == 11
    assert result.reclaimed_bytes == 31
    assert result.stdout == "pruned"
    assert keep.exists()


def test_uv_prune_refuses_unrecognized_directory(tmp_path: Path) -> None:
    env, _ = _layout(tmp_path)
    arbitrary = tmp_path / "not-the-uv-cache"
    arbitrary.mkdir()

    with pytest.raises(ValueError, match="已审计"):
        prune_uv_cache(arbitrary, env)


def test_uv_prune_refuses_while_uv_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    monkeypatch.setattr(uv_maintenance, "uv_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="uv 正在运行"):
        prune_uv_cache(cache, env)


def test_uv_prune_surfaces_vendor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, cache = _layout(tmp_path)
    monkeypatch.setattr(uv_maintenance, "uv_process_running", lambda: False)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["uv"], 2, stdout="", stderr="cache locked")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache locked"):
        prune_uv_cache(cache, env)
