from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import devclean.core.bazel_maintenance as bazel_maintenance
from devclean.core.bazel_maintenance import (
    BazelCleanMode,
    clean_bazel_workspace,
    inspect_bazel_workspace,
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MODULE.bazel").write_text('module(name = "demo")\n', encoding="utf-8")
    output_base = tmp_path / "bazel-output-base"
    output_base.mkdir()
    env = {"DEVCLEAN_BAZEL_EXE": "bazel-test"}
    return env, workspace, output_base


def _completed(command: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def _install_info_fake(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    output_base: Path,
    calls: list[list[str]],
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == workspace.resolve()
        calls.append(command)
        if command[-2:] == ["info", "workspace"]:
            return _completed(command, f"{workspace.resolve()}\n")
        if command[-2:] == ["info", "output_base"]:
            return _completed(command, f"{output_base.resolve()}\n")
        if command[-2:] == ["info", "release"]:
            return _completed(command, "release 9.0.0\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_inspect_uses_bazel_to_confirm_workspace_and_output_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, output_base = _layout(tmp_path)
    payload = output_base / "execroot" / "_main" / "bazel-out" / "artifact.obj"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 37)
    calls: list[list[str]] = []
    _install_info_fake(monkeypatch, workspace, output_base, calls)

    inventory = inspect_bazel_workspace(workspace, env)

    assert inventory.workspace == workspace.resolve()
    assert inventory.output_base == output_base.resolve()
    assert inventory.logical_bytes == 37
    assert inventory.executable == "bazel-test"
    assert inventory.release == "release 9.0.0"
    assert not inventory.recommended_clean
    assert inventory.expunge_user_review
    assert [call[-2:] for call in calls] == [
        ["info", "workspace"],
        ["info", "output_base"],
        ["info", "release"],
    ]


def test_workspace_without_repository_boundary_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "ordinary"
    directory.mkdir()

    with pytest.raises(ValueError, match="repository boundary"):
        inspect_bazel_workspace(directory, {"DEVCLEAN_BAZEL_EXE": "bazel-test"})


def test_mismatched_bazel_workspace_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, _ = _layout(tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command[-2:] == ["info", "workspace"]
        return _completed(command, f"{other}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="different workspace"):
        inspect_bazel_workspace(workspace, env)


def test_clean_delegates_to_bazel_and_keeps_external_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, output_base = _layout(tmp_path)
    build = output_base / "execroot" / "_main" / "bazel-out" / "artifact.obj"
    external = output_base / "external" / "dep" / "archive.zip"
    build.parent.mkdir(parents=True)
    external.parent.mkdir(parents=True)
    build.write_bytes(b"b" * 53)
    external.write_bytes(b"e" * 19)
    calls: list[list[str]] = []
    monkeypatch.setattr(bazel_maintenance, "bazel_client_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == workspace.resolve()
        calls.append(command)
        if command[-2:] == ["info", "workspace"]:
            return _completed(command, f"{workspace.resolve()}\n")
        if command[-2:] == ["info", "output_base"]:
            return _completed(command, f"{output_base.resolve()}\n")
        if command[-2:] == ["info", "release"]:
            return _completed(command, "release 9.0.0\n")
        if command[-1:] == ["clean"]:
            build.unlink()
            return _completed(command, "clean complete\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_bazel_workspace(workspace, BazelCleanMode.CLEAN, env)

    assert result.command == ("bazel-test", "clean")
    assert result.before_bytes == 72
    assert result.after_bytes == 19
    assert result.reclaimed_bytes == 53
    assert external.exists()
    assert not build.exists()
    assert calls[-1] == ["bazel-test", "clean"]


def test_expunge_is_explicit_and_removes_entire_output_base_through_bazel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, output_base = _layout(tmp_path)
    payload = output_base / "external" / "dep" / "source.tar"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 71)
    monkeypatch.setattr(bazel_maintenance, "bazel_client_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["cwd"] == workspace.resolve()
        if command[-2:] == ["info", "workspace"]:
            return _completed(command, f"{workspace.resolve()}\n")
        if command[-2:] == ["info", "output_base"]:
            return _completed(command, f"{output_base.resolve()}\n")
        if command[-2:] == ["info", "release"]:
            return _completed(command, "release 9.0.0\n")
        if command[-2:] == ["clean", "--expunge"]:
            shutil.rmtree(output_base)
            return _completed(command, "expunged\n")
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = clean_bazel_workspace(workspace, BazelCleanMode.EXPUNGE, env)

    assert result.command == ("bazel-test", "clean", "--expunge")
    assert result.before_bytes == 71
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 71
    assert not output_base.exists()


def test_cleanup_refuses_while_other_bazel_client_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, output_base = _layout(tmp_path)
    calls: list[list[str]] = []
    _install_info_fake(monkeypatch, workspace, output_base, calls)
    monkeypatch.setattr(bazel_maintenance, "bazel_client_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="already running"):
        clean_bazel_workspace(workspace, BazelCleanMode.CLEAN, env)

    assert all(call[-1] != "clean" for call in calls)


def test_large_output_base_recommends_normal_clean_not_expunge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, workspace, output_base = _layout(tmp_path)
    calls: list[list[str]] = []
    _install_info_fake(monkeypatch, workspace, output_base, calls)
    monkeypatch.setattr(bazel_maintenance, "_directory_bytes", lambda path: 3 * 1024**3)

    inventory = inspect_bazel_workspace(workspace, env)

    assert inventory.recommended_clean
    assert inventory.expunge_user_review
