from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from devclean.core.triage import ReviewLane, triage_file
from devclean.platform.windows.filesystem import (
    FILE_ATTRIBUTE_COMPRESSED,
    FILE_ATTRIBUTE_SPARSE_FILE,
    read_file_metadata,
)
from devclean.scanner import BoundaryReason, ScanOptions, ScanRecordKind, scan_roots


@pytest.mark.skipif(os.name != "nt", reason="Windows junction integration test")
def test_windows_junction_is_reported_but_never_descended(tmp_path: Path) -> None:
    scan_root = tmp_path / "scan-root"
    target = tmp_path / "junction-target"
    junction = scan_root / "junction"
    scan_root.mkdir()
    target.mkdir()
    (target / "protected-canary.txt").write_text("keep", encoding="utf-8")

    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")

    records = list(scan_roots([scan_root]))
    junction_record = next(
        record for record in records if Path(record.path).name == "junction"
    )

    assert junction_record.kind is ScanRecordKind.BOUNDARY
    assert junction_record.boundary_reason is BoundaryReason.REPARSE_POINT
    assert not any("protected-canary.txt" in record.path for record in records)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction-loop integration test")
def test_windows_junction_loop_is_bounded_at_first_reparse_point(tmp_path: Path) -> None:
    scan_root = tmp_path / "loop-root"
    nested = scan_root / "nested"
    back_edge = nested / "back-to-root"
    nested.mkdir(parents=True)
    (scan_root / "visible-canary.txt").write_text("keep", encoding="utf-8")

    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(back_edge), str(scan_root)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"junction-loop creation unavailable: {result.stderr or result.stdout}")

    try:
        records = list(scan_roots([scan_root]))
    finally:
        # Remove only the fixture junction itself so temporary-directory cleanup
        # never has an opportunity to interpret the intentional back edge.
        os.rmdir(back_edge)

    back_edge_records = [
        record for record in records if Path(record.path).name == "back-to-root"
    ]
    assert len(back_edge_records) == 1
    assert back_edge_records[0].kind is ScanRecordKind.BOUNDARY
    assert back_edge_records[0].boundary_reason is BoundaryReason.REPARSE_POINT
    assert sum(Path(record.path).name == "visible-canary.txt" for record in records) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows metadata integration test")
def test_windows_regular_file_has_no_reparse_tag(tmp_path: Path) -> None:
    target = tmp_path / "ordinary-file.txt"
    target.write_text("ordinary", encoding="utf-8")

    metadata = read_file_metadata(target)

    assert metadata.is_reparse_point is False
    assert metadata.reparse_tag is None


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only scan integration test")
def test_windows_old_temp_scan_and_classification_never_mutate_canary(tmp_path: Path) -> None:
    temp_root = tmp_path / "DevClean-read-only-temp"
    temp_root.mkdir()
    target = temp_root / "DevClean-canary.tmp"
    payload = "this old canary must survive every scan"
    target.write_text(payload, encoding="utf-8")
    old_time = time.time() - (8 * 24 * 60 * 60)
    os.utime(target, (old_time, old_time))
    before = read_file_metadata(target)

    records = list(scan_roots((temp_root,), ScanOptions(include_directories=False)))
    record = next(record for record in records if record.path == str(target))
    item = triage_file(record, temp_root=temp_root)
    after = read_file_metadata(target)

    assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE
    assert target.read_text(encoding="utf-8") == payload
    assert after.identity == before.identity
    assert after.logical_size == before.logical_size
    assert after.last_write_time_ns == before.last_write_time_ns


@pytest.mark.skipif(os.name != "nt", reason="Windows sparse-file integration test")
def test_windows_sparse_file_keeps_logical_and_allocated_sizes_distinct(
    tmp_path: Path,
) -> None:
    sparse = tmp_path / "sparse-fixture.bin"
    sparse.write_bytes(b"start")
    fsutil = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "fsutil.exe"
    result = subprocess.run(
        [str(fsutil), "sparse", "setflag", str(sparse)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"sparse-file fixture unavailable: {result.stderr or result.stdout}")
    with sparse.open("r+b") as stream:
        stream.seek(8 * 1024 * 1024)
        stream.write(b"end")

    metadata = read_file_metadata(sparse)
    if not metadata.attributes & FILE_ATTRIBUTE_SPARSE_FILE:
        pytest.skip("filesystem did not retain the sparse-file attribute")
    assert metadata.logical_size > 8 * 1024 * 1024
    assert metadata.allocation_size < metadata.logical_size


@pytest.mark.skipif(os.name != "nt", reason="Windows compression integration test")
def test_windows_compressed_file_reports_compressed_metadata(tmp_path: Path) -> None:
    compressed = tmp_path / "compressed-fixture.txt"
    compressed.write_bytes(b"DevClean-compression-fixture\n" * 131_072)
    compact = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "compact.exe"
    result = subprocess.run(
        [str(compact), "/C", "/I", "/Q", str(compressed)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"NTFS compression fixture unavailable: {result.stderr or result.stdout}")

    metadata = read_file_metadata(compressed)
    if not metadata.attributes & FILE_ATTRIBUTE_COMPRESSED:
        pytest.skip("filesystem did not retain the compressed-file attribute")
    assert metadata.logical_size == compressed.stat().st_size
    assert metadata.allocation_size <= metadata.logical_size


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path integration test")
def test_windows_unicode_space_and_long_path_is_observed_without_mutation(
    tmp_path: Path,
) -> None:
    current = tmp_path / "中文 用户"
    try:
        for index in range(6):
            current /= f"segment {index}-" + "x" * 32
        current.mkdir(parents=True)
        target = current / "模型 cache 文件.txt"
        target.write_text("canary", encoding="utf-8")
    except OSError as error:
        pytest.skip(f"long-path fixture unavailable: {error}")

    records = list(scan_roots([tmp_path]))
    target_record = next(record for record in records if record.path == str(target))
    assert target_record.kind is ScanRecordKind.FILE
    assert target.read_text(encoding="utf-8") == "canary"
