from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.go_maintenance as go_maintenance
from devclean.core.go_maintenance import (
    GoCacheKind,
    clean_go_cache,
    inventory_go_storage,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    build_cache = tmp_path / "go-build"
    module_cache = tmp_path / "go-mod"
    build_cache.mkdir()
    module_cache.mkdir()
    (build_cache / "artifact.bin").write_bytes(b"a" * 11)
    (module_cache / "module.zip").write_bytes(b"b" * 17)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "APPDATA": str(tmp_path / "Roaming"),
        "GOCACHE": str(build_cache),
        "GOMODCACHE": str(module_cache),
        "DEVCLEAN_GO_EXE": "C:/Go/bin/go.exe",
    }
    return env, build_cache, module_cache


def test_go_inventory_is_read_only_and_sums_cache_bytes(tmp_path: Path) -> None:
    env, build_cache, module_cache = _layout(tmp_path)

    inventory = inventory_go_storage(env)

    by_kind = {entry.kind: entry for entry in inventory.caches}
    assert by_kind[GoCacheKind.BUILD].path == build_cache
    assert by_kind[GoCacheKind.BUILD].logical_bytes == 11
    assert by_kind[GoCacheKind.MODULE].path == module_cache
    assert by_kind[GoCacheKind.MODULE].logical_bytes == 17
    assert inventory.total_cache_bytes == 28
    assert (build_cache / "artifact.bin").exists()
    assert (module_cache / "module.zip").exists()


def test_go_cleanup_rejects_arbitrary_directory(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    arbitrary = tmp_path / "cache"
    arbitrary.mkdir()

    with pytest.raises(ValueError, match="已审计"):
        clean_go_cache(GoCacheKind.BUILD, arbitrary, env)


def test_go_cleanup_requires_existing_exact_root(tmp_path: Path) -> None:
    env, build_cache, _ = _layout(tmp_path)
    for child in build_cache.iterdir():
        child.unlink()
    build_cache.rmdir()

    with pytest.raises(FileNotFoundError, match="不存在"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)


def test_go_cleanup_blocks_when_process_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)


def test_go_build_cache_cleanup_scopes_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        run_env = kwargs["env"]
        assert isinstance(run_env, dict)
        calls.append((command, run_env))
        (build_cache / "artifact.bin").unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_go_cache(GoCacheKind.BUILD, build_cache, env)

    assert calls[0][0] == ["C:/Go/bin/go.exe", "clean", "-cache"]
    assert calls[0][1]["GOCACHE"] == str(build_cache)
    assert calls[0][1]["GOMODCACHE"] == str(Path(env["GOMODCACHE"]))
    assert result.before_bytes == 11
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 11


def test_go_module_cache_cleanup_scopes_vendor_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, module_cache = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        run_env = kwargs["env"]
        assert isinstance(run_env, dict)
        calls.append((command, run_env))
        (module_cache / "module.zip").unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_go_cache(GoCacheKind.MODULE, module_cache, env)

    assert calls[0][0] == ["C:/Go/bin/go.exe", "clean", "-modcache"]
    assert calls[0][1]["GOMODCACHE"] == str(module_cache)
    assert result.before_bytes == 17
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 17


def test_go_cleanup_surfaces_vendor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        del command, kwargs
        return SimpleNamespace(returncode=1, stdout="", stderr="cache busy")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache busy"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
