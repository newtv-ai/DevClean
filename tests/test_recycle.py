from __future__ import annotations

from pathlib import Path

import pytest

from reclaimer.core import recycle
from reclaimer.core.recycle import RecycleRefusal, recycle_targets, targets_from_records
from reclaimer.platform.windows.filesystem import FileSystemMetadata
from reclaimer.platform.windows.recycle_bin import RecycleBinError, _absolute_source


def _record(path: Path) -> dict[str, object]:
    return {
        "candidate_id": "candidate_recycle_fixture",
        "adapter_id": "filesystem",
        "path": str(path),
        "logical_size": {"value": 7, "confidence": "EXACT"},
        "identity": {
            "volume_serial": "000000000000002a",
            "file_id": "ab" * 16,
            "file_id_kind": "file_id_128",
            "link_count": 1,
            "attributes": 32,
            "reparse_tag": None,
            "creation_time_ns": 100,
            "last_write_time_ns": 200,
        },
    }


def _metadata(*, logical_size: int = 7) -> FileSystemMetadata:
    return FileSystemMetadata(
        is_directory=False,
        logical_size=logical_size,
        allocation_size=4096,
        volume_serial=42,
        file_id="ab" * 16,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=32,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def test_recycle_selection_requires_stable_exact_filesystem_record(tmp_path: Path) -> None:
    target = targets_from_records([_record(tmp_path / "cache.bin")])

    assert target[0].path == tmp_path / "cache.bin"
    assert target[0].file_id_kind == "file_id_128"


def test_recycle_selection_rejects_protected_credential_name(tmp_path: Path) -> None:
    with pytest.raises(RecycleRefusal, match="credential"):
        targets_from_records([_record(tmp_path / ".env.production")])


def test_recycle_selection_allows_a_different_fixed_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recycle, "data_dir", lambda: Path(r"C:\Reclaimer\state"))

    target = targets_from_records([_record(Path(r"G:\cache\payload.bin"))])

    assert target[0].path.drive.casefold() == "g:"


def test_recycle_preflight_rejects_changed_file_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = targets_from_records([_record(tmp_path / "cache.bin")])
    monkeypatch.setattr(recycle, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(recycle, "read_file_metadata", lambda _path: _metadata(logical_size=8))

    with pytest.raises(RecycleRefusal, match="changed since it was scanned"):
        recycle.preflight_targets(target)


def test_recycle_revalidates_immediately_before_recycler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "cache.bin"
    path.write_bytes(b"fixture")
    target = targets_from_records([_record(path)])
    monkeypatch.setattr(recycle, "is_local_fixed_path", lambda _path: True)
    calls = 0

    def metadata(_path: Path) -> FileSystemMetadata:
        nonlocal calls
        calls += 1
        return _metadata()

    monkeypatch.setattr(recycle, "read_file_metadata", metadata)

    def recycler(recycle_path: Path) -> None:
        recycle_path.unlink()

    recycled = recycle_targets(target, recycler)

    assert [item.candidate_id for item in recycled] == ["candidate_recycle_fixture"]
    assert calls == 2
    assert not path.exists()


def test_shell_recycle_rejects_extended_path_prefix() -> None:
    with pytest.raises(RecycleBinError, match="ordinary absolute"):
        _absolute_source(r"\\?\C:\fixture\cache.bin")
