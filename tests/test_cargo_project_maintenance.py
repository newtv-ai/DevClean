from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import devclean.core.cargo_project_maintenance as cargo_project
from devclean.core.cargo_project_maintenance import (
    clean_cargo_workspace,
    inspect_cargo_workspace,
)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Cargo.toml").write_text(
        '[workspace]\nmembers = []\nresolver = "2"\n',
        encoding="utf-8",
    )
    return root


def _completed(command: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, stdout=stdout, stderr="")


def _metadata_result(
    command: tuple[str, ...],
    workspace: Path,
    target: Path,
) -> subprocess.CompletedProcess[str]:
    return _completed(
        command,
        json.dumps(
            {
                "packages": [],
                "workspace_members": [],
                "workspace_default_members": [],
                "resolve": None,
                "target_directory": str(target),
                "workspace_root": str(workspace),
                "metadata": {},
                "version": 1,
            }
        ),
    )


def test_inspect_uses_cargo_metadata_for_exact_workspace_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    target = root / "custom-target"
    target.mkdir()
    (target / "artifact.bin").write_bytes(b"x" * 31)
    seen: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        seen.append(command)
        assert workspace == root
        if "metadata" in command:
            return _metadata_result(command, root, target)
        return _completed(command, "cargo 1.90.0 (test)\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)
    monkeypatch.setattr(cargo_project, "is_local_fixed_path", lambda path: True)

    inventory = inspect_cargo_workspace(root, {"DEVCLEAN_CARGO_EXE": "cargo-test"})

    assert inventory.workspace == root.resolve()
    assert inventory.manifest == root.resolve() / "Cargo.toml"
    assert inventory.target_directory == target
    assert inventory.logical_bytes == 31
    assert inventory.executable == "cargo-test"
    assert inventory.version == "cargo 1.90.0 (test)"
    assert inventory.deletion_supported
    assert inventory.user_review_required
    assert seen[0][:6] == (
        "cargo-test",
        "metadata",
        "--format-version",
        "1",
        "--no-deps",
        "--manifest-path",
    )


def test_inspect_requires_selected_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _workspace(tmp_path)
    actual = tmp_path / "actual-workspace"
    actual.mkdir()
    target = actual / "target"

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del workspace, environment, timeout
        if "metadata" in command:
            return _metadata_result(command, actual, target)
        return _completed(command, "cargo test\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)

    with pytest.raises(ValueError, match="workspace 根目录"):
        inspect_cargo_workspace(selected)


def test_external_or_shared_target_is_report_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    target = tmp_path / "shared-target"
    target.mkdir()

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del workspace, environment, timeout
        if "metadata" in command:
            return _metadata_result(command, root, target)
        return _completed(command, "cargo test\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)
    monkeypatch.setattr(cargo_project, "is_local_fixed_path", lambda path: True)

    inventory = inspect_cargo_workspace(root)

    assert not inventory.deletion_supported


def test_clean_pins_verified_target_directory_and_uses_cargo_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    target = root / "target"
    target.mkdir()
    (target / "debug" / "artifact.exe").parent.mkdir()
    (target / "debug" / "artifact.exe").write_bytes(b"x" * 47)
    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        commands.append(command)
        assert workspace == root
        if "metadata" in command:
            return _metadata_result(command, root, target)
        if command[1:] == ("--version",):
            return _completed(command, "cargo 1.90.0 (test)\n")
        assert command[1] == "clean"
        assert command[-2:] == ("--target-dir", str(target))
        shutil.rmtree(target)
        return _completed(command, "Removed 1 file\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)
    monkeypatch.setattr(cargo_project, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(cargo_project, "clear_cargo_process_cache", lambda: None)
    monkeypatch.setattr(cargo_project, "cargo_process_running", lambda: False)

    result = clean_cargo_workspace(root, {"DEVCLEAN_CARGO_EXE": "cargo-test"})

    assert result.target_directory == target
    assert result.before_bytes == 47
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 47
    assert result.command == (
        "cargo-test",
        "clean",
        "--manifest-path",
        str(root / "Cargo.toml"),
        "--target-dir",
        str(target),
    )
    assert not target.exists()


def test_clean_refuses_when_cargo_tooling_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    target = root / "target"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"x")

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del workspace, environment, timeout
        if "metadata" in command:
            return _metadata_result(command, root, target)
        return _completed(command, "cargo test\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)
    monkeypatch.setattr(cargo_project, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(cargo_project, "clear_cargo_process_cache", lambda: None)
    monkeypatch.setattr(cargo_project, "cargo_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        clean_cargo_workspace(root)

    assert (target / "keep.bin").exists()


def test_large_target_only_marks_user_review_as_worthwhile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _workspace(tmp_path)
    target = root / "target"
    target.mkdir()

    def fake_run(
        command: tuple[str, ...],
        workspace: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del workspace, environment, timeout
        if "metadata" in command:
            return _metadata_result(command, root, target)
        return _completed(command, "cargo test\n")

    monkeypatch.setattr(cargo_project, "_run_cargo", fake_run)
    monkeypatch.setattr(cargo_project, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(cargo_project, "_directory_bytes", lambda path: 3 * 1024**3)

    inventory = inspect_cargo_workspace(root)

    assert inventory.worth_reviewing
    assert inventory.user_review_required


def test_missing_cargo_manifest_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "not-cargo"
    root.mkdir()

    with pytest.raises(ValueError, match="Cargo.toml"):
        inspect_cargo_workspace(root)
