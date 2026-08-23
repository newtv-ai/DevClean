from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import devclean.core.nuget_maintenance as nuget_maintenance
from devclean.core.nuget_maintenance import (
    NuGetLocalKind,
    NuGetMaintenanceLane,
    clear_nuget_local,
    inventory_nuget_storage,
    nuget_maintenance_lane,
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
    dotnet = tmp_path / "dotnet-test.exe"
    dotnet.write_bytes(b"fake-dotnet")
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "APPDATA": str(tmp_path / "Roaming"),
        "TEMP": str(tmp_path / "Temp"),
        "NUGET_PACKAGES": str(roots[NuGetLocalKind.GLOBAL_PACKAGES]),
        "NUGET_HTTP_CACHE_PATH": str(roots[NuGetLocalKind.HTTP_CACHE]),
        "NUGET_SCRATCH": str(roots[NuGetLocalKind.TEMP]),
        "NUGET_PLUGINS_CACHE_PATH": str(roots[NuGetLocalKind.PLUGINS_CACHE]),
        "DEVCLEAN_DOTNET_EXE": str(dotnet),
    }
    return env, roots


def _override_for(kind: NuGetLocalKind) -> str:
    return {
        NuGetLocalKind.GLOBAL_PACKAGES: "NUGET_PACKAGES",
        NuGetLocalKind.HTTP_CACHE: "NUGET_HTTP_CACHE_PATH",
        NuGetLocalKind.TEMP: "NUGET_SCRATCH",
        NuGetLocalKind.PLUGINS_CACHE: "NUGET_PLUGINS_CACHE_PATH",
    }[kind]


def _vendor_list_or_none(
    command: list[str],
    kwargs: dict[str, Any],
    *,
    kind: NuGetLocalKind,
    root: Path,
    reported_root: Path | None = None,
) -> subprocess.CompletedProcess[str] | None:
    process_env = kwargs["env"]
    assert isinstance(process_env, dict)
    assert os.path.normcase(process_env[_override_for(kind)]) == os.path.normcase(
        str(root.resolve())
    )
    if command[-2:] != ["--list", "--force-english-output"]:
        return None
    assert command[1:4] == ["nuget", "locals", kind.value]
    shown = (reported_root or root).resolve()
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=f"{kind.value}: {shown}\n",
        stderr="",
    )


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


def test_nuget_cache_lanes_are_local_and_do_not_require_ai() -> None:
    assert (
        nuget_maintenance_lane(NuGetLocalKind.GLOBAL_PACKAGES)
        is NuGetMaintenanceLane.USER_REVIEW
    )
    for kind in (
        NuGetLocalKind.HTTP_CACHE,
        NuGetLocalKind.TEMP,
        NuGetLocalKind.PLUGINS_CACHE,
    ):
        assert nuget_maintenance_lane(kind) is NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE


def test_nuget_inventory_recommends_only_worthwhile_deterministic_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    sizes = {
        roots[NuGetLocalKind.GLOBAL_PACKAGES]: 8 * 1024**3,
        roots[NuGetLocalKind.HTTP_CACHE]: 128 * 1024**2,
        roots[NuGetLocalKind.TEMP]: 1 * 1024**2,
        roots[NuGetLocalKind.PLUGINS_CACHE]: 32 * 1024**2,
    }
    monkeypatch.setattr(
        nuget_maintenance,
        "_directory_bytes",
        lambda path: sizes[path],
    )

    inventory = inventory_nuget_storage(env)
    by_kind = {entry.kind: entry for entry in inventory.locals}

    assert not by_kind[NuGetLocalKind.GLOBAL_PACKAGES].recommended
    assert by_kind[NuGetLocalKind.GLOBAL_PACKAGES].lane is NuGetMaintenanceLane.USER_REVIEW
    assert by_kind[NuGetLocalKind.HTTP_CACHE].recommended
    assert not by_kind[NuGetLocalKind.TEMP].recommended
    assert by_kind[NuGetLocalKind.PLUGINS_CACHE].recommended
    assert inventory.recommended_bytes == 160 * 1024**2
    assert inventory.deterministic_bytes == 161 * 1024**2


@pytest.mark.parametrize(
    "kind",
    [
        NuGetLocalKind.GLOBAL_PACKAGES,
        NuGetLocalKind.HTTP_CACHE,
        NuGetLocalKind.TEMP,
        NuGetLocalKind.PLUGINS_CACHE,
    ],
)
def test_nuget_clear_confirms_exact_vendor_root_then_removes_the_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: NuGetLocalKind,
) -> None:
    env, roots = _layout(tmp_path)
    root = roots[kind]
    payload = root / "payload.bin"
    payload.write_bytes(b"x" * 31)
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            assert kwargs["timeout"] == 60
            return probe
        assert command == [
            str(Path(env["DEVCLEAN_DOTNET_EXE"]).resolve()),
            "nuget",
            "locals",
            kind.value,
            "--clear",
            "--force-english-output",
        ]
        assert kwargs["timeout"] == 600
        shutil.rmtree(root)
        return subprocess.CompletedProcess(command, 0, stdout="cleared", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clear_nuget_local(kind, root, env)

    expected_list = [
        str(Path(env["DEVCLEAN_DOTNET_EXE"]).resolve()),
        "nuget",
        "locals",
        kind.value,
        "--list",
        "--force-english-output",
    ]
    expected_clear = [
        str(Path(env["DEVCLEAN_DOTNET_EXE"]).resolve()),
        "nuget",
        "locals",
        kind.value,
        "--clear",
        "--force-english-output",
    ]
    assert calls == [expected_list, expected_list, expected_clear]
    assert result.kind is kind
    assert result.path == root.resolve()
    assert result.before_bytes == 31
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 31
    assert result.command == tuple(expected_clear)
    assert result.stdout == "cleared"
    assert not root.exists()


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


def test_nuget_clear_rechecks_process_immediately_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.HTTP_CACHE
    root = roots[kind]
    states = iter((False, True))
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: next(states))
    clear_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clear_called
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            return probe
        clear_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="正在运行"):
        clear_nuget_local(kind, root, env)
    assert not clear_called
    assert root.exists()


def test_nuget_clear_fails_closed_when_vendor_lists_different_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.PLUGINS_CACHE
    root = roots[kind]
    wrong = tmp_path / "other-plugins"
    wrong.mkdir()
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)
    clear_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clear_called
        probe = _vendor_list_or_none(
            command,
            kwargs,
            kind=kind,
            root=root,
            reported_root=wrong,
        )
        if probe is not None:
            return probe
        clear_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="未确认所选"):
        clear_nuget_local(kind, root, env)
    assert not clear_called
    assert root.exists()


def test_nuget_clear_revalidates_dotnet_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.TEMP
    root = roots[kind]
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)
    real_identity = nuget_maintenance._path_identity
    cli_calls = 0
    clear_called = False

    def identity(
        path: Path,
        *,
        expect_directory: bool,
        label: str,
    ) -> nuget_maintenance.NuGetPathIdentity:
        nonlocal cli_calls
        current = real_identity(path, expect_directory=expect_directory, label=label)
        if label == ".NET CLI":
            cli_calls += 1
            if cli_calls >= 2:
                return replace(
                    current,
                    last_write_time_ns=(current.last_write_time_ns or 0) + 1,
                )
        return current

    monkeypatch.setattr(nuget_maintenance, "_path_identity", identity)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clear_called
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            return probe
        clear_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="CLI 身份发生变化"):
        clear_nuget_local(kind, root, env)
    assert not clear_called
    assert root.exists()


def test_nuget_clear_revalidates_root_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.HTTP_CACHE
    root = roots[kind]
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)
    real_identity = nuget_maintenance._path_identity
    root_calls = 0
    clear_called = False

    def identity(
        path: Path,
        *,
        expect_directory: bool,
        label: str,
    ) -> nuget_maintenance.NuGetPathIdentity:
        nonlocal root_calls
        current = real_identity(path, expect_directory=expect_directory, label=label)
        if label == "NuGet http-cache":
            root_calls += 1
            if root_calls >= 2:
                return replace(current, file_id=current.file_id + "-changed")
        return current

    monkeypatch.setattr(nuget_maintenance, "_path_identity", identity)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal clear_called
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            return probe
        clear_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="NuGet http-cache 身份发生变化"):
        clear_nuget_local(kind, root, env)
    assert not clear_called
    assert root.exists()


def test_nuget_clear_requires_vendor_root_removal_postcondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.HTTP_CACHE
    root = roots[kind]
    payload = root / "still-here.bin"
    payload.write_bytes(b"keep")
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            return probe
        return subprocess.CompletedProcess(command, 0, stdout="cleared", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="目标根目录仍存在"):
        clear_nuget_local(kind, root, env)
    assert payload.exists()


def test_nuget_clear_surfaces_vendor_failure_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, roots = _layout(tmp_path)
    kind = NuGetLocalKind.PLUGINS_CACHE
    root = roots[kind]
    payload = root / "keep.bin"
    payload.write_bytes(b"keep")
    monkeypatch.setattr(nuget_maintenance, "nuget_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probe = _vendor_list_or_none(command, kwargs, kind=kind, root=root)
        if probe is not None:
            return probe
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr="cache locked",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="cache locked"):
        clear_nuget_local(kind, root, env)
    assert payload.exists()
