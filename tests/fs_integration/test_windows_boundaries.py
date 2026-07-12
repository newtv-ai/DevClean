from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from reclaimer.core.auto_clean import (
    permanently_clean_model_approved_record,
    permanently_clean_temp_record,
)
from reclaimer.core.triage import ReviewLane, triage_file
from reclaimer.platform.windows.filesystem import (
    FILE_ATTRIBUTE_COMPRESSED,
    FILE_ATTRIBUTE_SPARSE_FILE,
    read_file_metadata,
)
from reclaimer.platform.windows.permanent_delete import PermanentDeleteRefusal
from reclaimer.scanner import BoundaryReason, ScanOptions, ScanRecordKind, scan_roots


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


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-delete integration test")
def test_windows_old_temp_canary_is_permanently_cleaned_after_handle_revalidation() -> None:
    """Only a unique test file under the active user Temp root is deleted."""

    with tempfile.TemporaryDirectory(
        prefix="reclaimer-auto-clean-", dir=tempfile.gettempdir()
    ) as temporary_directory:
        temp_root = Path(temporary_directory)
        target = temp_root / "reclaimer-canary.tmp"
        target.write_text("delete this test canary", encoding="utf-8")
        old_time = time.time() - (8 * 24 * 60 * 60)
        os.utime(target, (old_time, old_time))
        record = next(
            record
            for record in scan_roots((temp_root,), ScanOptions(include_directories=False))
            if record.path == str(target)
        )

        assert triage_file(record, temp_root=temp_root).lane is ReviewLane.AUTO_CLEAN
        permanently_clean_temp_record(record, temp_root=temp_root)

        assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-delete integration test")
def test_windows_changed_temp_canary_is_refused_and_kept() -> None:
    with tempfile.TemporaryDirectory(
        prefix="reclaimer-auto-clean-", dir=tempfile.gettempdir()
    ) as temporary_directory:
        temp_root = Path(temporary_directory)
        target = temp_root / "changed-canary.tmp"
        target.write_text("before scan", encoding="utf-8")
        old_time = time.time() - (8 * 24 * 60 * 60)
        os.utime(target, (old_time, old_time))
        record = next(
            record
            for record in scan_roots((temp_root,), ScanOptions(include_directories=False))
            if record.path == str(target)
        )
        target.write_text("changed after scan", encoding="utf-8")

        with pytest.raises(PermanentDeleteRefusal, match="changed since classification"):
            permanently_clean_temp_record(record, temp_root=temp_root)

        assert target.read_text(encoding="utf-8") == "changed after scan"


@pytest.mark.skipif(os.name != "nt", reason="Windows AI-approved delete integration test")
def test_windows_model_approved_cache_canary_is_permanently_cleaned() -> None:
    with tempfile.TemporaryDirectory(
        prefix="reclaimer-ai-review-", dir=tempfile.gettempdir()
    ) as temporary_directory:
        cache_root = Path(temporary_directory) / "pip"
        cache_root.mkdir()
        target = cache_root / "cache-canary.whl"
        target.write_text("delete this exact reviewed cache canary", encoding="utf-8")
        record = next(
            record
            for record in scan_roots((cache_root,), ScanOptions(include_directories=False))
            if record.path == str(target)
        )

        assert triage_file(record, temp_root=cache_root.parent).lane is ReviewLane.AI_REVIEW
        permanently_clean_model_approved_record(record)

        assert not target.exists()


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
    compressed.write_bytes(b"reclaimer-compression-fixture\n" * 131_072)
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
