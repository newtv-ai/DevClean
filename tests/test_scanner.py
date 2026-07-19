from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

import devclean.scanner.filesystem as scanner_module
from devclean.platform.windows import (
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FileSystemMetadata,
    is_cloud_reparse_tag,
    read_file_metadata,
)
from devclean.scanner import (
    BoundaryReason,
    CancellationToken,
    ScanOptions,
    ScanRecordKind,
    ScanStats,
    scan_roots,
)


def _by_name(records: list[scanner_module.ScanRecord]) -> dict[str, scanner_module.ScanRecord]:
    return {Path(record.path).name: record for record in records}


def test_scan_options_reject_invalid_progress_interval() -> None:
    with pytest.raises(ValueError, match="progress_interval"):
        ScanOptions(progress_interval=0)


def test_scan_options_reject_invalid_hardlink_capacity() -> None:
    with pytest.raises(ValueError, match="max_hardlink_identities"):
        ScanOptions(max_hardlink_identities=0)


def test_scan_streams_files_directories_and_final_progress(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "alpha.bin").write_bytes(b"alpha")
    (nested / "beta.bin").write_bytes(b"beta-data")
    progress: list[ScanStats] = []

    records = list(
        scan_roots(
            [tmp_path],
            ScanOptions(progress_interval=1),
            progress=lambda stats: progress.append(stats),
        )
    )
    paths = _by_name(records)

    assert paths[tmp_path.name].kind is ScanRecordKind.DIRECTORY
    assert paths["nested"].kind is ScanRecordKind.DIRECTORY
    assert paths["alpha.bin"].logical_size == 5
    assert paths["beta.bin"].logical_size == 9
    assert all(record.kind is not ScanRecordKind.ERROR for record in records)
    assert progress[-1].completed is True
    assert progress[-1].cancelled is False
    assert progress[-1].files == 2
    assert progress[-1].directories == 2
    assert progress[-1].logical_bytes == 14


def test_hardlink_allocation_is_counted_once(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    linked = tmp_path / "linked.bin"
    original.write_bytes(b"x" * 8192)
    try:
        os.link(original, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    records = [
        record
        for record in scan_roots([tmp_path])
        if record.kind is ScanRecordKind.FILE
    ]
    by_name = _by_name(records)
    original_record = by_name["original.bin"]
    linked_record = by_name["linked.bin"]

    assert original_record.file_id == linked_record.file_id
    assert original_record.volume_serial == linked_record.volume_serial
    assert original_record.link_count is not None and original_record.link_count >= 2
    assert sum(record.hardlink_duplicate for record in records) == 1
    raw_sizes = [record.raw_allocated_size for record in records]
    if all(size is not None for size in raw_sizes):
        assert sum(record.allocated_size or 0 for record in records) == raw_sizes[0]


def test_hardlink_dedup_can_be_disabled(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    linked = tmp_path / "linked.bin"
    original.write_bytes(b"x" * 4096)
    try:
        os.link(original, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")

    records = [
        record
        for record in scan_roots(
            [tmp_path], ScanOptions(deduplicate_hardlinks=False)
        )
        if record.kind is ScanRecordKind.FILE
    ]

    assert not any(record.hardlink_duplicate for record in records)
    assert [record.allocated_size for record in records] == [
        record.raw_allocated_size for record in records
    ]


def test_hardlink_identity_memory_is_bounded_and_degrades_to_unknown(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.bin"
    first_link = tmp_path / "first-link.bin"
    second = tmp_path / "second.bin"
    second_link = tmp_path / "second-link.bin"
    first.write_bytes(b"a" * 4096)
    second.write_bytes(b"b" * 4096)
    try:
        os.link(first, first_link)
        os.link(second, second_link)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    progress: list[ScanStats] = []

    records = [
        record
        for record in scan_roots(
            [tmp_path],
            ScanOptions(max_hardlink_identities=1),
            progress=lambda stats: progress.append(stats),
        )
        if record.kind is ScanRecordKind.FILE
    ]

    assert sum(record.hardlink_duplicate for record in records) == 1
    assert sum(record.allocation_uncertain for record in records) == 2
    assert sum(record.allocated_size is None for record in records) == 2
    assert progress[-1].hardlink_identity_capacity_reached is True
    assert progress[-1].allocation_unknown_files == 2


def test_symlink_is_boundary_and_target_is_not_descended(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = tmp_path / "target"
    root.mkdir()
    target.mkdir()
    (target / "must-not-appear.txt").write_text("protected", encoding="utf-8")
    link = root / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    records = list(scan_roots([root]))
    link_record = next(record for record in records if Path(record.path).name == "link")

    assert link_record.kind is ScanRecordKind.BOUNDARY
    assert link_record.boundary_reason is BoundaryReason.REPARSE_POINT
    assert not any("must-not-appear.txt" in record.path for record in records)


def test_cloud_placeholder_is_boundary_without_descending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud_dir = tmp_path / "cloud"
    cloud_dir.mkdir()
    (cloud_dir / "remote-only.bin").write_bytes(b"not actually read")
    original_reader = scanner_module.read_file_metadata

    def fake_reader(path: str | os.PathLike[str]) -> FileSystemMetadata:
        metadata = original_reader(path)
        if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(cloud_dir)):
            return replace(
                metadata,
                attributes=(metadata.attributes or 0)
                | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
                is_cloud_placeholder=True,
            )
        return metadata

    monkeypatch.setattr(scanner_module, "read_file_metadata", fake_reader)
    records = list(scan_roots([tmp_path]))
    cloud_record = next(record for record in records if Path(record.path).name == "cloud")

    assert cloud_record.kind is ScanRecordKind.BOUNDARY
    assert cloud_record.boundary_reason is BoundaryReason.CLOUD_FILES_PLACEHOLDER
    assert not any("remote-only.bin" in record.path for record in records)


def test_metadata_error_is_recorded_and_scan_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "blocked.bin"
    visible = tmp_path / "visible.bin"
    blocked.write_bytes(b"blocked")
    visible.write_bytes(b"visible")
    original_reader = scanner_module.read_file_metadata
    progress: list[ScanStats] = []

    def fake_reader(path: str | os.PathLike[str]) -> FileSystemMetadata:
        if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(blocked)):
            raise PermissionError(errno_eacces(), "access denied", os.fspath(path))
        return original_reader(path)

    monkeypatch.setattr(scanner_module, "read_file_metadata", fake_reader)
    records = list(
        scan_roots([tmp_path], progress=lambda stats: progress.append(stats))
    )
    by_name = _by_name(records)

    assert by_name["blocked.bin"].kind is ScanRecordKind.ERROR
    assert by_name["visible.bin"].kind is ScanRecordKind.FILE
    assert progress[-1].errors == 1
    assert progress[-1].inaccessible == 1


def errno_eacces() -> int:
    # Keep the exception portable while still exercising Windows error-code
    # accounting through the shared EACCES value.
    import errno

    return errno.EACCES


def test_cancellation_stops_between_records_and_closes_cleanly(tmp_path: Path) -> None:
    for index in range(20):
        (tmp_path / f"file-{index:02}.bin").write_bytes(b"x")
    token = CancellationToken()
    progress: list[ScanStats] = []

    def on_progress(stats: ScanStats) -> None:
        progress.append(stats)
        if stats.files >= 1 and not stats.completed:
            token.cancel()

    records = list(
        scan_roots(
            [tmp_path],
            ScanOptions(progress_interval=1),
            cancel=token,
            progress=on_progress,
        )
    )

    assert sum(record.kind is ScanRecordKind.FILE for record in records) == 1
    assert progress[-1].cancelled is True
    assert progress[-1].completed is False


def test_cancellation_stops_new_traversal_within_two_seconds_under_slow_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(200):
        (tmp_path / f"slow-{index:03}.bin").write_bytes(b"x")
    token = CancellationToken()
    original_reader = scanner_module.read_file_metadata
    fifth_read_started = Event()
    read_calls = 0
    records: list[scanner_module.ScanRecord] = []
    failure: list[BaseException] = []

    def slow_reader(path: str | os.PathLike[str]) -> FileSystemMetadata:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 5:
            fifth_read_started.set()
        time.sleep(0.05)
        return original_reader(path)

    def run_scan() -> None:
        try:
            records.extend(scan_roots([tmp_path], cancel=token))
        except BaseException as error:
            failure.append(error)

    monkeypatch.setattr(scanner_module, "read_file_metadata", slow_reader)
    worker = Thread(target=run_scan, name="DevClean-slow-cancellation-test")
    worker.start()
    assert fifth_read_started.wait(timeout=2), "slow fixture did not reach its cancellation point"
    cancelled_at = time.monotonic()
    token.cancel()
    worker.join(timeout=2)
    elapsed = time.monotonic() - cancelled_at

    assert not worker.is_alive()
    assert failure == []
    assert elapsed < 2
    assert read_calls == 5
    assert len(records) < 200


def test_directory_records_can_be_suppressed(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "payload.bin").write_bytes(b"payload")

    records = list(scan_roots([tmp_path], ScanOptions(include_directories=False)))

    assert [record.kind for record in records] == [ScanRecordKind.FILE]
    assert Path(records[0].path).name == "payload.bin"


def test_current_platform_metadata_is_observational(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"sample")

    metadata = read_file_metadata(sample)

    assert metadata.logical_size == 6
    assert metadata.is_directory is False
    assert metadata.is_reparse_point is False
    assert metadata.is_cloud_placeholder is False
    assert metadata.link_count is None or metadata.link_count >= 1
    if metadata.file_id is not None:
        assert metadata.volume_serial is not None


def test_missing_root_uses_invalid_handle_path_and_becomes_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    records = list(scan_roots([missing]))

    assert len(records) == 1
    assert records[0].kind is ScanRecordKind.ERROR
    assert records[0].path == os.path.abspath(missing)


@pytest.mark.parametrize(
    "tag",
    [0x9000001A, 0x9000101A, 0x9000C01A, 0x80000021],
)
def test_cloud_reparse_tag_family(tag: int) -> None:
    assert is_cloud_reparse_tag(tag)


def test_unrelated_reparse_tag_is_not_cloud() -> None:
    assert not is_cloud_reparse_tag(0xA0000003)
