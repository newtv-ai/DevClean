from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import devclean.core.go_maintenance as go_maintenance
from devclean.core.go_maintenance import (
    GoCacheKind,
    GoMaintenanceLane,
    clean_go_cache,
    go_maintenance_lane,
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


def test_go_inventory_is_read_only_and_separates_decision_lanes(
    tmp_path: Path,
) -> None:
    env, build_cache, module_cache = _layout(tmp_path)

    inventory = inventory_go_storage(env)

    by_kind = {entry.kind: entry for entry in inventory.caches}
    assert by_kind[GoCacheKind.BUILD].path == build_cache
    assert by_kind[GoCacheKind.BUILD].logical_bytes == 11
    assert by_kind[GoCacheKind.BUILD].lane is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
    assert not by_kind[GoCacheKind.BUILD].recommended
    assert by_kind[GoCacheKind.MODULE].path == module_cache
    assert by_kind[GoCacheKind.MODULE].logical_bytes == 17
    assert by_kind[GoCacheKind.MODULE].lane is GoMaintenanceLane.USER_REVIEW
    assert not by_kind[GoCacheKind.MODULE].recommended
    assert inventory.total_cache_bytes == 28
    assert inventory.deterministic_bytes == 11
    assert inventory.recommended_bytes == 0
    assert (build_cache / "artifact.bin").exists()
    assert (module_cache / "module.zip").exists()


def test_go_lanes_are_local_and_never_need_ai() -> None:
    assert (
        go_maintenance_lane(GoCacheKind.BUILD)
        is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
    )
    assert go_maintenance_lane(GoCacheKind.MODULE) is GoMaintenanceLane.USER_REVIEW


def test_go_large_build_cache_is_worthwhile_default_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, module_cache = _layout(tmp_path)
    sizes = {
        build_cache: 2 * 1024**3,
        module_cache: 20 * 1024**3,
    }
    monkeypatch.setattr(go_maintenance, "_directory_bytes", lambda path: sizes[path])

    inventory = inventory_go_storage(env)
    by_kind = {entry.kind: entry for entry in inventory.caches}

    assert by_kind[GoCacheKind.BUILD].recommended
    assert not by_kind[GoCacheKind.MODULE].recommended
    assert inventory.recommended_bytes == 2 * 1024**3


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


@pytest.mark.parametrize(
    ("kind", "variable", "flag"),
    [
        (GoCacheKind.BUILD, "GOCACHE", "-cache"),
        (GoCacheKind.MODULE, "GOMODCACHE", "-modcache"),
    ],
)
def test_go_cleanup_confirms_exact_vendor_cache_then_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: GoCacheKind,
    variable: str,
    flag: str,
) -> None:
    env, build_cache, module_cache = _layout(tmp_path)
    cache = build_cache if kind is GoCacheKind.BUILD else module_cache
    payload = next(cache.iterdir())
    before = payload.stat().st_size
    calls: list[list[str]] = []
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        run_env = kwargs["env"]
        assert isinstance(run_env, dict)
        assert run_env[variable] == str(cache)
        if command[1] == "env":
            assert command == ["C:/Go/bin/go.exe", "env", variable]
            return subprocess.CompletedProcess(command, 0, stdout=str(cache), stderr="")
        assert command == ["C:/Go/bin/go.exe", "clean", flag]
        payload.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="cleaned", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_go_cache(kind, cache, env)

    assert calls == [
        ["C:/Go/bin/go.exe", "env", variable],
        ["C:/Go/bin/go.exe", "clean", flag],
    ]
    assert result.before_bytes == before
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == before
    assert result.command == ("C:/Go/bin/go.exe", "clean", flag)
    assert result.output == "cleaned"


def test_go_cleanup_fails_closed_when_vendor_reports_different_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    calls = 0

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=str(tmp_path / "other-cache"),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="未确认"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert calls == 1


def test_go_cleanup_surfaces_vendor_failure_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    payload = build_cache / "artifact.bin"
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "env":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=str(build_cache),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="cache busy")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache busy"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert payload.exists()
