from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.core.meson_project_maintenance as meson_project
from devclean.core.meson_project_maintenance import (
    inspect_meson_build,
    remove_meson_build_directory,
)
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactRootBoundary,
)


def _source_and_build(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "meson.build").write_text("project('demo', 'c')\n", encoding="utf-8")
    build = source / "out-custom"
    (build / "meson-private").mkdir(parents=True)
    (build / "meson-private" / "coredata.dat").write_bytes(b"configured")
    (build / "artifact.bin").write_bytes(b"x" * 41)
    return source, build


def _completed(command: tuple[str, ...], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(command), 0, stdout=stdout, stderr="")


def _snapshot(seed: int) -> ExactDirectorySnapshot:
    return ExactDirectorySnapshot(
        volume_serial=100 + seed,
        file_id=f"id-{seed}",
        file_id_kind="128",
        creation_time_ns=1000 + seed,
    )


def _ordinary_metadata() -> SimpleNamespace:
    return SimpleNamespace(is_directory=True, is_reparse_point=False)


def _install_portable_identity_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meson_project, "read_file_metadata", lambda path: _ordinary_metadata())
    monkeypatch.setattr(meson_project, "is_local_fixed_path", lambda path: True)

    def fake_snapshot(path: Path, label: str) -> ExactDirectorySnapshot:
        del label
        return _snapshot(abs(hash(str(path.resolve()))) % 100000)

    monkeypatch.setattr(meson_project, "_exact_directory_snapshot", fake_snapshot)
    monkeypatch.setattr(
        meson_project,
        "_exact_root_boundary",
        lambda path: ExactRootBoundary(
            path=path,
            volume_serial=1,
            file_id="boundary",
            file_id_kind="128",
        ),
    )


def _install_meson_stub(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    build: Path,
    *,
    buildsystem_files: list[Path] | None = None,
) -> list[tuple[str, ...]]:
    seen: list[tuple[str, ...]] = []
    files = buildsystem_files or [source / "meson.build"]

    def fake_run(
        command: tuple[str, ...],
        cwd: Path,
        environment: object,
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del environment, timeout
        seen.append(command)
        assert cwd == source.resolve()
        if command[1:] == ("--version",):
            return _completed(command, "1.8.0\n")
        assert command[1:3] == ("introspect", "--buildsystem-files")
        assert command[-1] == str(build.resolve())
        return _completed(command, json.dumps([str(path.resolve()) for path in files]))

    monkeypatch.setattr(meson_project, "_run_meson", fake_run)
    return seen


def test_inspect_binds_exact_configured_build_to_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    nested = source / "subdir"
    nested.mkdir()
    nested_build_file = nested / "meson.build"
    nested_build_file.write_text("# nested\n", encoding="utf-8")
    _install_portable_identity_stubs(monkeypatch)
    seen = _install_meson_stub(
        monkeypatch,
        source,
        build,
        buildsystem_files=[source / "meson.build", nested_build_file],
    )

    inventory = inspect_meson_build(
        source,
        build,
        {"DEVCLEAN_MESON_EXE": "meson-test"},
    )

    assert inventory.source_root == source.resolve()
    assert inventory.build_root == build.resolve()
    assert inventory.logical_bytes == 41 + len(b"configured")
    assert inventory.executable == "meson-test"
    assert inventory.version == "1.8.0"
    assert inventory.deletion_supported
    assert inventory.user_review_required
    assert not inventory.worth_reviewing
    assert seen[1] == (
        "meson-test",
        "introspect",
        "--buildsystem-files",
        str(build.resolve()),
    )


def test_arbitrary_build_named_directory_without_meson_marker_has_no_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "meson.build").write_text("project('demo')\n", encoding="utf-8")
    build = source / "build"
    build.mkdir()
    _install_portable_identity_stubs(monkeypatch)

    with pytest.raises(ValueError, match=r"coredata\.dat"):
        inspect_meson_build(source, build)


def test_source_build_mismatch_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    actual_source = tmp_path / "actual-source"
    actual_source.mkdir()
    (actual_source / "meson.build").write_text("project('other')\n", encoding="utf-8")
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(
        monkeypatch,
        source,
        build,
        buildsystem_files=[actual_source / "meson.build"],
    )

    with pytest.raises(ValueError, match="不匹配"):
        inspect_meson_build(source, build)


def test_selecting_configured_subproject_as_source_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    top = tmp_path / "top"
    top.mkdir()
    (top / "meson.build").write_text("project('top')\n", encoding="utf-8")
    sub = top / "subprojects" / "child"
    sub.mkdir(parents=True)
    (sub / "meson.build").write_text("project('child')\n", encoding="utf-8")
    build = top / "out"
    (build / "meson-private").mkdir(parents=True)
    (build / "meson-private" / "coredata.dat").write_bytes(b"configured")
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(
        monkeypatch,
        sub,
        build,
        buildsystem_files=[top / "meson.build", sub / "meson.build"],
    )

    with pytest.raises(ValueError, match="顶层源码"):
        inspect_meson_build(sub, build)


def test_build_root_that_contains_source_is_refused_before_meson_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = tmp_path / "container-build"
    (build / "meson-private").mkdir(parents=True)
    (build / "meson-private" / "coredata.dat").write_bytes(b"configured")
    source = build / "source"
    source.mkdir()
    (source / "meson.build").write_text("project('demo')\n", encoding="utf-8")
    _install_portable_identity_stubs(monkeypatch)

    with pytest.raises(ValueError, match="包含源码"):
        inspect_meson_build(source, build)


def test_non_local_build_is_report_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(monkeypatch, source, build)
    monkeypatch.setattr(
        meson_project,
        "is_local_fixed_path",
        lambda path: path.resolve() != build.resolve(),
    )

    inventory = inspect_meson_build(source, build)

    assert not inventory.deletion_supported


def test_remove_refuses_while_meson_or_build_tooling_is_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(monkeypatch, source, build)
    monkeypatch.setattr(meson_project, "meson_build_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        remove_meson_build_directory(source, build)

    assert (build / "artifact.bin").exists()


def test_remove_revalidates_identity_before_exact_tree_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(monkeypatch, source, build)
    monkeypatch.setattr(meson_project, "meson_build_process_running", lambda: False)

    calls = 0
    stable_source = _snapshot(11)
    stable_build = _snapshot(22)
    changed_build = _snapshot(23)

    def changing_snapshot(path: Path, label: str) -> ExactDirectorySnapshot:
        nonlocal calls
        del label
        calls += 1
        if path.resolve() == source.resolve():
            return stable_source
        # Two inspections each read build identity twice. The final pre-purge
        # build identity read is deliberately changed.
        return changed_build if calls >= 10 else stable_build

    monkeypatch.setattr(meson_project, "_exact_directory_snapshot", changing_snapshot)

    with pytest.raises(RuntimeError, match="执行前发生变化"):
        remove_meson_build_directory(source, build)

    assert build.exists()


def test_remove_purges_only_exact_verified_build_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    source_file = source / "main.c"
    source_file.write_text("int main(void){return 0;}\n", encoding="utf-8")
    sibling = source / "build-backup"
    sibling.mkdir()
    (sibling / "keep.bin").write_bytes(b"keep")
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(monkeypatch, source, build)
    monkeypatch.setattr(meson_project, "meson_build_process_running", lambda: False)
    purged: list[Path] = []

    def fake_purge(
        root: Path,
        expected: ExactDirectorySnapshot,
        boundary: ExactRootBoundary,
    ) -> DirectoryPurgeResult:
        del expected
        purged.append(root)
        assert boundary.path == build.resolve().parent
        shutil.rmtree(root)
        return DirectoryPurgeResult(
            root_path=str(root),
            files_removed=2,
            links_removed=0,
            directories_removed=2,
            bytes_removed=49,
            root_absent=True,
            completed=True,
        )

    monkeypatch.setattr(meson_project, "purge_exact_directory_tree", fake_purge)

    result = remove_meson_build_directory(source, build)

    assert purged == [build.resolve()]
    assert not build.exists()
    assert source_file.exists()
    assert (sibling / "keep.bin").read_bytes() == b"keep"
    assert result.build_root == build.resolve()
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == result.before_bytes


def test_large_build_tree_is_only_marked_worth_user_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    _install_portable_identity_stubs(monkeypatch)
    _install_meson_stub(monkeypatch, source, build)
    monkeypatch.setattr(meson_project, "_directory_bytes", lambda path: 3 * 1024**3)

    inventory = inspect_meson_build(source, build)

    assert inventory.worth_reviewing
    assert inventory.user_review_required


def test_root_reparse_input_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, build = _source_and_build(tmp_path)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == build)
    monkeypatch.setattr(Path, "is_junction", lambda self: False)

    with pytest.raises(ValueError, match="symlink/junction/reparse"):
        inspect_meson_build(source, build)
