from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.nuget_maintenance as nuget_maintenance
from devclean.core.nuget_maintenance import (
    NuGetLocalKind,
    clear_nuget_local,
    inventory_nuget_storage,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], dict[NuGetLocalKind, Path]]:
    roots = {
        NuGetLocalKind.GLOBAL_PACKAGES: tmp_path / "packages",
        NuGetLocalKind.HTTP_CACHE: tmp_path / "http-cache",
        NuGetLocalKind.TEMP: tmp_path / "scratch",
        NuGetLocalKind.PLUGINS_CACHE: tmp_path / "plugins-cache",
    }
    for root in roots.values():
        root.mkdir(parents=True)
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "APPDATA": str(tmp_path / "Roaming"),
        "TEMP": str(tmp_path / "Temp"),
        "NUGET_PACKAGES": str(roots[NuGetLocalKind.GLOBAL_PACKAGES]),
        "NUGET_HTTP_CACHE_PATH": str(roots[NuGetLocalKind.HTTP_CACHE]),
        "NUGET_SCRATCH": str(roots[NuGetLocalKind.TEMP]),
        "NUGET_PLUGINS_CACHE_PATH": str(roots[NuGetLocalKind.PLUGINS_CACHE]),
        "DEVCLEAN_DOTNET_EXE": "dotnet-test",
    }
    return env, roots


def test_nuget_inventory_is_read_only_and_sums_all_locals(tmp_path: Path) -> None:
    env, roots = _layout(tmp_path)
    size = 0
    for index, root in enumerate(roots.values(), start=1):
        payload = b"x" * (index * 11)
        (root / "payload.bin").write_bytes(payload)
        size += len(payload)

    inventory = inventory_nuget_storage(env)

    assert len(inventory.locals) == 4
    assert inventory.total_local_bytes == size
    assert all(entry.exists for entry in inventory.locals)
    assert all((entry.path / "payload.bin").exists() for entry in inventory.locals)


@pytest.mark.parametrize(
    ("kind", "override"),
    [
        (NuGetLocalKind.GLOBAL_PACKAGES, "NUGET_PACKAGES"),
        (NuGetLocalKind.HTTP_CACHE, "NUGET_HTTP_CACHE_PATH"),
        (NuGetLocalKind.TEMP, "NUGET_SCRATCH"),
        (NuGetLocalKind.PLUGINS_CACHE, "NUGET_PLUGINS_CACHE_PATH"),
    ],
)
def test_nuget_clear_uses_vendor_command_and_exact_root_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: NuGetLocalKind,
    override: str,
) -> None:
    env, roots = _layout(tmp_path)
    root = roots[kind]
    payload = root / "payload.bin"
    payload.write_bytes(b"x" * 31)
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert command == [
            "dotnet-test",
            "nuget",
            "locals",
            kind.value,
            "--clear",
            "--force-english-output",
        ]
        assert check is False
        assert capture_output is True
        assert text is True
        assert timeout == 600
        assert os.path.normcase(env[override]) == os.path.normcase(str(root))
        payload.unlink()
        return subprocess.CompletedProcess(command, 0, stdout="cleared", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clear_nuget_local(kind, root, env)

    assert result.kind is kind
    assert result.path == root
    assert result.before_bytes == 31
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 31
    assert result.stdout == "cleared"


def test_nuget_clear_refuses_wrong_kind_or_unrecognized_root(tmp_path: Path) -> None:
    env, roots = _layout(tmp_path)
    with pytest.raises(ValueError, match="已审计"):
        clear_nuget_local(
            NuGetLocalKind.HTTP_CACHE,
            roots[NuGetLocalKind.GLOBAL_PACKAGES],
            env,
        )


def test_nuget_clear_refuses_while_restore_or_build_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        clear_nuget_local(
            NuGetLocalKind.HTTP_CACHE,
            roots[NuGetLocalKind.HTTP_CACHE],
            env,
        )


def test_nuget_clear_surfaces_vendor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["dotnet-test"],
            2,
            stdout="",
            stderr="cache locked",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache locked"):
        clear_nuget_local(
            NuGetLocalKind.PLUGINS_CACHE,
            roots[NuGetLocalKind.PLUGINS_CACHE],
            env,
        )
