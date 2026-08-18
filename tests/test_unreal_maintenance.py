from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.unreal_maintenance as unreal_maintenance
from devclean.core.unreal_maintenance import (
    UnrealStorageKind,
    inventory_unreal_storage,
    run_unreal_ddc_cleanup,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    program_files = tmp_path / "Program Files"
    engine_root = program_files / "Epic Games" / "UE_5.3"
    editor = engine_root / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    editor.parent.mkdir(parents=True)
    editor.write_bytes(b"exe")
    ddc = engine_root / "Engine" / "DerivedDataCache"
    ddc.mkdir(parents=True)
    local = tmp_path / "LocalAppData"
    zen = local / "UnrealEngine" / "Common" / "Zen" / "Data"
    zen.mkdir(parents=True)
    env = {
        "PROGRAMFILES": str(program_files),
        "LOCALAPPDATA": str(local),
    }
    return env, editor, ddc, zen


def test_inventory_discovers_default_engine_ddc_and_zen_without_raw_authority(
    tmp_path: Path,
) -> None:
    env, editor, ddc, zen = _layout(tmp_path)
    (ddc / "legacy.ddc").write_bytes(b"a" * 31)
    (zen / "data.bin").write_bytes(b"b" * 47)

    inventory = inventory_unreal_storage(env)

    assert len(inventory.engines) == 1
    assert inventory.engines[0].editor_cmd == editor.resolve()
    by_kind = {entry.kind: entry for entry in inventory.stores if entry.exists}
    assert by_kind[UnrealStorageKind.FILESYSTEM_DDC].path == ddc.resolve()
    assert by_kind[UnrealStorageKind.FILESYSTEM_DDC].logical_bytes == 31
    assert by_kind[UnrealStorageKind.ZEN_DATA].path == zen.resolve()
    assert by_kind[UnrealStorageKind.ZEN_DATA].logical_bytes == 47
    assert all(not entry.raw_delete_allowed for entry in inventory.stores)
    assert inventory.total_known_bytes == 78


def test_inventory_honors_zen_and_local_ddc_overrides_without_deletion_authority(
    tmp_path: Path,
) -> None:
    env, _, _, _ = _layout(tmp_path)
    zen_override = tmp_path / "ZenOverride"
    local_override = tmp_path / "LocalDDC"
    zen_override.mkdir()
    local_override.mkdir()
    (zen_override / "zen.bin").write_bytes(b"z" * 13)
    (local_override / "cache.bin").write_bytes(b"c" * 17)
    env["UE-ZenDataPath"] = str(zen_override)
    env["UE-LocalDataCachePath"] = str(local_override)

    inventory = inventory_unreal_storage(env)
    entries = {entry.path: entry for entry in inventory.stores}

    assert entries[zen_override.resolve()].kind is UnrealStorageKind.ZEN_DATA
    assert entries[local_override.resolve()].kind is UnrealStorageKind.CONFIGURED_LOCAL
    assert not entries[zen_override.resolve()].raw_delete_allowed
    assert not entries[local_override.resolve()].raw_delete_allowed


def test_ddc_cleanup_runs_engine_commandlet_and_measures_observed_reclaim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, editor, ddc, _ = _layout(tmp_path)
    stale = ddc / "stale.ddc"
    stale.write_bytes(b"x" * 101)
    calls: list[list[str]] = []
    monkeypatch.setattr(unreal_maintenance, "unreal_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        assert command[0] == str(editor.resolve())
        assert "-run=DDCCleanup" in command
        assert "-NoShaderCompile" in command
        assert "-NullRHI" in command
        assert "-unattended" in command
        stale.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="DDC cleanup complete\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_unreal_ddc_cleanup(editor.resolve(), env)

    assert len(calls) == 1
    assert result.editor_cmd == editor.resolve()
    assert result.before_known_bytes == 101
    assert result.after_known_bytes == 0
    assert result.observed_reclaimed_bytes == 101
    assert result.output == "DDC cleanup complete"


def test_ddc_cleanup_refuses_arbitrary_editor_binary(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    arbitrary = tmp_path / "UnrealEditor-Cmd.exe"
    arbitrary.write_bytes(b"exe")

    with pytest.raises(ValueError, match="不是当前已发现"):
        run_unreal_ddc_cleanup(arbitrary, env)


def test_ddc_cleanup_refuses_while_unreal_build_activity_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, editor, _, _ = _layout(tmp_path)
    monkeypatch.setattr(unreal_maintenance, "unreal_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        run_unreal_ddc_cleanup(editor.resolve(), env)


def test_large_known_ddc_storage_is_recommended_only_as_benefit_heuristic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _, _ = _layout(tmp_path)
    monkeypatch.setattr(unreal_maintenance, "_directory_bytes", lambda path: 3 * 1024**3)

    inventory = inventory_unreal_storage(env)

    assert inventory.recommended


def test_missing_unreal_install_has_no_maintenance_candidate(tmp_path: Path) -> None:
    inventory = inventory_unreal_storage(
        {
            "PROGRAMFILES": str(tmp_path / "Program Files"),
            "LOCALAPPDATA": str(tmp_path / "Local"),
        }
    )

    assert inventory.engines == ()
    assert not inventory.recommended


def test_local_cache_none_override_is_ignored(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    env["UE-LocalDataCachePath"] = "None"

    inventory = inventory_unreal_storage(env)

    assert all(entry.kind is not UnrealStorageKind.CONFIGURED_LOCAL for entry in inventory.stores)


def test_engine_root_list_accepts_multiple_explicit_installations(tmp_path: Path) -> None:
    roots: list[Path] = []
    for version in ("UE_5.3", "UE_5.8"):
        root = tmp_path / version
        editor = root / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
        editor.parent.mkdir(parents=True)
        editor.write_bytes(b"exe")
        roots.append(root)
    env = {
        "DEVCLEAN_UNREAL_ENGINE_ROOTS": os.pathsep.join(str(root) for root in roots),
    }

    inventory = inventory_unreal_storage(env)

    assert len(inventory.engines) == 2
