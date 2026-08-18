from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from devclean.core.git_repository_maintenance import (
    inspect_git_repository,
    preview_git_lfs_prune,
    run_git_automatic_maintenance,
    run_git_lfs_prune,
)


def _result(
    arguments: tuple[str, ...],
    code: int = 0,
    output: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git-test", *arguments],
        returncode=code,
        stdout=output,
        stderr="",
    )


def _install_fake_git(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    maintenance_needed: bool = True,
    alternate: Path | None = None,
    custom_lfs_storage: str | None = None,
    lfs_used: bool = True,
    calls: list[tuple[str, ...]] | None = None,
) -> None:
    git_dir = root / ".git"
    objects = git_dir / "objects"
    lfs = git_dir / "lfs"

    def fake(
        executable: str,
        workspace: Path,
        arguments: tuple[str, ...],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del executable, workspace, environment, timeout
        if calls is not None:
            calls.append(arguments)
        if arguments == ("rev-parse", "--show-toplevel"):
            return _result(arguments, output=f"{root}\n")
        if arguments == ("rev-parse", "--is-bare-repository"):
            return _result(arguments, output="false\n")
        if arguments == ("rev-parse", "--absolute-git-dir"):
            return _result(arguments, output=f"{git_dir}\n")
        if arguments == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            return _result(arguments, output=f"{git_dir}\n")
        if arguments == (
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "objects",
        ):
            return _result(arguments, output=f"{objects}\n")
        if arguments == ("count-objects", "-v"):
            extra = f"alternate: {alternate}\n" if alternate is not None else ""
            return _result(arguments, output=f"count: 4\nsize: 1\n{extra}")
        if arguments == ("--version",):
            return _result(arguments, output="git version 2.54.0\n")
        if arguments == ("maintenance", "is-needed", "--auto"):
            return _result(arguments, code=0 if maintenance_needed else 1)
        if arguments == ("maintenance", "run", "--auto"):
            return _result(arguments, output="maintenance complete\n")
        if arguments == ("lfs", "version"):
            return _result(arguments, output="git-lfs/3.7.0\n")
        if arguments == ("config", "--get", "lfs.storage"):
            if custom_lfs_storage is None:
                return _result(arguments, code=1)
            return _result(arguments, output=f"{custom_lfs_storage}\n")
        if arguments == ("lfs", "env"):
            media = (
                Path(custom_lfs_storage)
                if custom_lfs_storage is not None
                else lfs
            )
            return _result(arguments, output=f"LocalMediaDir={media}\n")
        if arguments == ("lfs", "ls-files", "--name-only"):
            return _result(arguments, output="asset.bin\n" if lfs_used else "")
        if arguments[:2] == ("lfs", "prune"):
            return _result(arguments, output="prune complete\n")
        raise AssertionError(f"unexpected Git arguments: {arguments}")

    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance._run_git_allow_status",
        fake,
    )
    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance.is_local_fixed_path",
        lambda path: True,
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "lfs" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "pack.bin").write_bytes(b"g" * 17)
    (root / ".git" / "lfs" / "objects" / "large.bin").write_bytes(b"l" * 29)
    return root


def test_inspect_requires_exact_worktree_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _repo(tmp_path)
    reported = tmp_path / "other"
    reported.mkdir()

    def fake(
        executable: str,
        workspace: Path,
        arguments: tuple[str, ...],
        environment: Mapping[str, str] | None,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del executable, workspace, environment, timeout
        assert arguments == ("rev-parse", "--show-toplevel")
        return _result(arguments, output=f"{reported}\n")

    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance._run_git_allow_status",
        fake,
    )

    with pytest.raises(ValueError, match="不是 Git worktree 根目录"):
        inspect_git_repository(selected, {"DEVCLEAN_GIT_EXE": "git-test"})


def test_inspect_exposes_vendor_maintenance_and_default_lfs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _install_fake_git(monkeypatch, root)

    inventory = inspect_git_repository(root, {"DEVCLEAN_GIT_EXE": "git-test"})

    assert inventory.version == "git version 2.54.0"
    assert inventory.object_bytes == 17
    assert inventory.maintenance_supported
    assert inventory.maintenance_needed is True
    assert inventory.maintenance_executable
    assert inventory.lfs.available
    assert inventory.lfs.used
    assert inventory.lfs.logical_bytes == 29
    assert not inventory.lfs.custom_storage
    assert inventory.lfs.prune_supported


def test_alternate_object_database_disables_git_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    alternate = tmp_path / "shared" / "objects"
    alternate.mkdir(parents=True)
    _install_fake_git(monkeypatch, root, alternate=alternate)

    inventory = inspect_git_repository(root, {"DEVCLEAN_GIT_EXE": "git-test"})

    assert inventory.alternates == (alternate,)
    assert not inventory.maintenance_executable
    assert "alternate" in inventory.maintenance_reason


def test_custom_lfs_storage_is_report_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    shared = tmp_path / "shared-lfs"
    shared.mkdir()
    (shared / "object.bin").write_bytes(b"x" * 11)
    _install_fake_git(monkeypatch, root, custom_lfs_storage=str(shared))

    inventory = inspect_git_repository(root, {"DEVCLEAN_GIT_EXE": "git-test"})

    assert inventory.lfs.custom_storage
    assert inventory.lfs.storage_dir == shared
    assert not inventory.lfs.prune_supported
    assert "共享" in inventory.lfs.reason


def test_git_maintenance_uses_only_vendor_auto_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    _install_fake_git(monkeypatch, root, calls=calls)
    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance.git_activity_running",
        lambda: False,
    )

    result = run_git_automatic_maintenance(
        root,
        {"DEVCLEAN_GIT_EXE": "git-test"},
    )

    assert result.command == ("git-test", "maintenance", "run", "--auto")
    assert ("maintenance", "run", "--auto") in calls
    assert result.before_bytes == 17
    assert result.after_bytes == 17


def test_lfs_preview_uses_verified_vendor_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    _install_fake_git(monkeypatch, root, calls=calls)

    preview = preview_git_lfs_prune(root, {"DEVCLEAN_GIT_EXE": "git-test"})

    assert "--dry-run" in preview.command
    assert "--verify-remote" in preview.command
    assert "--verify-unreachable" in preview.command
    assert "--when-unverified=halt" in preview.command
    assert "--force" not in preview.command
    assert preview.before_bytes == 29


def test_lfs_prune_never_uses_force_and_requires_remote_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []
    _install_fake_git(monkeypatch, root, calls=calls)
    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance.git_activity_running",
        lambda: False,
    )

    result = run_git_lfs_prune(root, {"DEVCLEAN_GIT_EXE": "git-test"})

    assert result.command[:3] == ("git-test", "lfs", "prune")
    assert "--verify-remote" in result.command
    assert "--verify-unreachable" in result.command
    assert "--when-unverified=halt" in result.command
    assert "--force" not in result.command
    assert result.before_bytes == 29
    assert result.after_bytes == 29


def test_mutation_refuses_when_git_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _install_fake_git(monkeypatch, root)
    monkeypatch.setattr(
        "devclean.core.git_repository_maintenance.git_activity_running",
        lambda: True,
    )

    with pytest.raises(RuntimeError, match="Git/Git LFS 活动"):
        run_git_automatic_maintenance(root, {"DEVCLEAN_GIT_EXE": "git-test"})
