from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.unity_project_maintenance as unity_maintenance
from devclean.core.unity_project_maintenance import (
    delete_unity_project_library,
    inspect_unity_project,
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "UnityProject"
    (root / "Assets").mkdir(parents=True)
    settings = root / "ProjectSettings"
    settings.mkdir()
    (settings / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.0.65f1\n",
        encoding="utf-8",
    )
    (root / "Packages").mkdir()
    return root


def test_inspect_requires_a_real_unity_project_boundary(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "Library").mkdir()

    with pytest.raises(ValueError, match="Unity 项目根目录"):
        inspect_unity_project(ordinary)


def test_inspect_reports_exact_library_size_and_editor_version(tmp_path: Path) -> None:
    root = _project(tmp_path)
    payload = root / "Library" / "Artifacts" / "artifact.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"x" * 47)

    inventory = inspect_unity_project(root)

    assert inventory.project_root == root.resolve()
    assert inventory.library == root.resolve() / "Library"
    assert inventory.logical_bytes == 47
    assert inventory.exists
    assert inventory.editor_version == "6000.0.65f1"
    assert inventory.user_review_required
    assert not inventory.worth_reviewing


def test_delete_removes_only_direct_library_and_preserves_project_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    source = root / "Assets" / "scene.unity"
    source.write_text("scene", encoding="utf-8")
    manifest = root / "Packages" / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    settings = root / "ProjectSettings" / "ProjectSettings.asset"
    settings.write_text("settings", encoding="utf-8")
    payload = root / "Library" / "Bee" / "build.bin"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"b" * 83)
    similarly_named = root / "LibraryBackup"
    similarly_named.mkdir()
    (similarly_named / "keep.bin").write_bytes(b"k" * 13)
    monkeypatch.setattr(unity_maintenance, "unity_editor_running", lambda: False)

    result = delete_unity_project_library(root)

    assert result.library == root.resolve() / "Library"
    assert result.before_bytes == 83
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 83
    assert not (root / "Library").exists()
    assert source.read_text(encoding="utf-8") == "scene"
    assert manifest.read_text(encoding="utf-8") == "{}\n"
    assert settings.read_text(encoding="utf-8") == "settings"
    assert (similarly_named / "keep.bin").read_bytes() == b"k" * 13


def test_delete_refuses_while_unity_editor_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    library = root / "Library"
    library.mkdir()
    (library / "state.bin").write_bytes(b"x")
    monkeypatch.setattr(unity_maintenance, "unity_editor_running", lambda: True)

    with pytest.raises(RuntimeError, match="Unity Editor 正在运行"):
        delete_unity_project_library(root)

    assert (library / "state.bin").exists()


def test_delete_rejects_non_directory_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "Library").write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(unity_maintenance, "unity_editor_running", lambda: False)

    with pytest.raises(ValueError, match="Library 不是目录"):
        delete_unity_project_library(root)


def test_large_library_is_only_marked_worth_user_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _project(tmp_path)
    (root / "Library").mkdir()
    monkeypatch.setattr(unity_maintenance, "_directory_bytes", lambda path: 6 * 1024**3)

    inventory = inspect_unity_project(root)

    assert inventory.worth_reviewing
    assert inventory.user_review_required
