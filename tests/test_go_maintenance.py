from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

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
    go_tool = tmp_path / "go.exe"
    go_tool.write_bytes(b"fake-go-tool")
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "APPDATA": str(tmp_path / "Roaming"),
        "GOCACHE": str(build_cache),
        "GOMODCACHE": str(module_cache),
        "DEVCLEAN_GO_EXE": str(go_tool),
    }
    return env, build_cache, module_cache


def _go_env_result(
    command: list[str],
    kwargs: dict[str, Any],
    *,
    cache_program: str = "",
    override_path: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    if command[1:3] != ["env", "-json"]:
        return None
    run_env = kwargs["env"]
    assert isinstance(run_env, dict)
    payload = {
        "GOCACHE": override_path or run_env["GOCACHE"],
        "GOCACHEPROG": cache_program,
        "GOMODCACHE": override_path or run_env["GOMODCACHE"],
    }
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=json.dumps(payload),
        stderr="",
    )


def _expected_clean_command(env: dict[str, str], kind: GoCacheKind) -> list[str]:
    return [
        env["DEVCLEAN_GO_EXE"],
        "clean",
        "-i=false",
        "-r=false",
        f"-cache={'true' if kind is GoCacheKind.BUILD else 'false'}",
        "-testcache=false",
        f"-modcache={'true' if kind is GoCacheKind.MODULE else 'false'}",
        "-fuzzcache=false",
        "-n=false",
    ]


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


def test_go_external_build_cache_program_is_report_only(tmp_path: Path) -> None:
    env, _, _ = _layout(tmp_path)
    env["GOCACHEPROG"] = "cache-helper --remote"

    inventory = inventory_go_storage(env)
    build = next(entry for entry in inventory.caches if entry.kind is GoCacheKind.BUILD)

    assert build.lane is GoMaintenanceLane.REPORT_ONLY
    assert not build.recommended
    assert "GOCACHEPROG" in build.reason
    assert inventory.deterministic_bytes == 0


def test_go_lanes_are_local_and_never_need_ai() -> None:
    assert (
        go_maintenance_lane(GoCacheKind.BUILD)
        is GoMaintenanceLane.DETERMINISTIC_CANDIDATE
    )
    assert (
        go_maintenance_lane(GoCacheKind.BUILD, "remote-helper")
        is GoMaintenanceLane.REPORT_ONLY
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


@pytest.mark.parametrize("kind", (GoCacheKind.BUILD, GoCacheKind.MODULE))
def test_go_cleanup_confirms_exact_vendor_cache_then_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: GoCacheKind,
) -> None:
    env, build_cache, module_cache = _layout(tmp_path)
    cache = build_cache if kind is GoCacheKind.BUILD else module_cache
    payload = next(cache.iterdir())
    before = payload.stat().st_size
    calls: list[list[str]] = []
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        assert command == _expected_clean_command(env, kind)
        payload.unlink()
        if kind is GoCacheKind.MODULE:
            cache.rmdir()
        return subprocess.CompletedProcess(command, 0, stdout="cleaned", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_go_cache(kind, cache, env)

    expected_env_command = [
        env["DEVCLEAN_GO_EXE"],
        "env",
        "-json",
        "GOCACHE",
        "GOCACHEPROG",
        "GOMODCACHE",
    ]
    assert calls == [
        expected_env_command,
        expected_env_command,
        _expected_clean_command(env, kind),
    ]
    assert result.before_bytes == before
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == before
    assert result.command == tuple(_expected_clean_command(env, kind))
    assert result.output == "cleaned"


def test_go_cleanup_explicit_flags_override_destructive_goflags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    env["GOFLAGS"] = "-modcache -fuzzcache -testcache -i -r -n"
    payload = build_cache / "artifact.bin"
    clean_commands: list[list[str]] = []
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        clean_commands.append(command)
        payload.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    clean_go_cache(GoCacheKind.BUILD, build_cache, env)

    assert clean_commands == [_expected_clean_command(env, GoCacheKind.BUILD)]
    assert "-cache=true" in clean_commands[0]
    for disabled in (
        "-i=false",
        "-r=false",
        "-testcache=false",
        "-modcache=false",
        "-fuzzcache=false",
        "-n=false",
    ):
        assert disabled in clean_commands[0]


def test_go_cleanup_external_cache_program_fails_before_vendor_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    env["GOCACHEPROG"] = "cache-helper --remote"

    def should_not_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Go CLI should not run for external build-cache mode")

    monkeypatch.setattr(subprocess, "run", should_not_run)

    with pytest.raises(RuntimeError, match="GOCACHEPROG"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)


def test_go_cleanup_fails_closed_when_vendor_reports_different_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    calls = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        config = _go_env_result(
            command,
            kwargs,
            override_path=str(tmp_path / "other-cache"),
        )
        assert config is not None
        return config

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="未确认"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert calls == 1


def test_go_cleanup_rechecks_process_state_immediately_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    states = iter((False, True))
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: next(states))
    clean_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clean_called
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        clean_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="正在运行"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert not clean_called


def test_go_cleanup_revalidates_cli_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    reviewed_tool = go_maintenance._resolve_go_tool(env)
    changed_tool = replace(
        reviewed_tool,
        last_write_time_ns=(reviewed_tool.last_write_time_ns or 0) + 1,
    )
    real_identity = go_maintenance._path_identity
    monkeypatch.setattr(go_maintenance, "_resolve_go_tool", lambda _environment: reviewed_tool)

    def identity(path: Path, *, expect_directory: bool, label: str):
        if Path(path) == reviewed_tool.path:
            return changed_tool
        return real_identity(path, expect_directory=expect_directory, label=label)

    monkeypatch.setattr(go_maintenance, "_path_identity", identity)
    clean_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clean_called
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        clean_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Go CLI 身份在执行前发生变化"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert not clean_called


def test_go_cleanup_revalidates_cache_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)
    real_identity = go_maintenance._path_identity
    calls_for_cache = 0

    def identity(path: Path, *, expect_directory: bool, label: str):
        nonlocal calls_for_cache
        current = real_identity(path, expect_directory=expect_directory, label=label)
        if Path(path) == build_cache:
            calls_for_cache += 1
            if calls_for_cache >= 2:
                return replace(current, file_id=current.file_id + "-changed")
        return current

    monkeypatch.setattr(go_maintenance, "_path_identity", identity)
    clean_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clean_called
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        clean_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache 身份在执行前发生变化"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert not clean_called


def test_go_cleanup_surfaces_vendor_failure_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, build_cache, _ = _layout(tmp_path)
    payload = build_cache / "artifact.bin"
    monkeypatch.setattr(go_maintenance, "go_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config = _go_env_result(command, kwargs)
        if config is not None:
            return config
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="cache busy")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache busy"):
        clean_go_cache(GoCacheKind.BUILD, build_cache, env)
    assert payload.exists()
