"""Benchmark bounded-batch Resource ingestion without creating target files.

This is an acceptance utility, not product cleanup code.  It creates a temporary
DevClean state database under ``--work-dir``, verifies the requested row count
and SQLite integrity, prints one JSON result, and removes only its own temporary
directory unless ``--retain`` is supplied.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import tempfile
import time
import tracemalloc
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from re import fullmatch

from devclean.core.models import (
    Confidence,
    ProvenanceClass,
    Resource,
    RiskTier,
    ScanStatus,
    SemanticType,
    SizeValue,
)
from devclean.core.state import StateStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--work-dir", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--retain", action="store_true")
    args = parser.parse_args()
    if args.count < 1 or args.batch_size < 1:
        parser.error("count and batch-size must be positive")
    if (
        not fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{6,127}", args.source_revision)
        or "replace" in args.source_revision.casefold()
    ):
        parser.error("source-revision is invalid or still a placeholder")
    if args.artifact.is_symlink():
        parser.error("artifact must not be a symlink")
    artifact = args.artifact.resolve(strict=True)
    if not artifact.is_file():
        parser.error("artifact must be an ordinary file")
    artifact_sha256 = _sha256(artifact)

    args.work_dir.mkdir(parents=True, exist_ok=True)
    if args.retain:
        run_root = Path(tempfile.mkdtemp(prefix="DevClean-benchmark-", dir=args.work_dir))
        result = run_benchmark(run_root, args.count, args.batch_size)
        result["retained_at"] = str(run_root)
    else:
        with tempfile.TemporaryDirectory(
            prefix="DevClean-benchmark-", dir=args.work_dir
        ) as temporary:
            result = run_benchmark(Path(temporary), args.count, args.batch_size)
    payload = {
        "schema_version": "1.0.0",
        "evidence_kind": "G1_STREAMING_STATE_BENCHMARK",
        "artifact_sha256": artifact_sha256,
        "source_revision": args.source_revision,
        "captured_at": datetime.now(UTC).isoformat(),
        "result": result,
        "verification": {
            "bounded_batching": args.batch_size <= 1024,
            "row_count_matches": result["stored"] == args.count,
            "sqlite_integrity_check": "ok" if result["integrity"] else "failed",
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run_benchmark(root: Path, count: int, batch_size: int) -> dict[str, object]:
    database = root / "state.db"
    tracemalloc.start()
    baseline_working_set, _ = _process_memory()
    started = time.perf_counter()
    with StateStore(database) as store:
        scan_id = store.create_scan(["synthetic://streaming-state-benchmark"])
        batch: list[Resource] = []
        for index in range(count):
            batch.append(_resource(index))
            if len(batch) == batch_size:
                store.add_resources(scan_id, batch)
                batch.clear()
        if batch:
            store.add_resources(scan_id, batch)
            batch.clear()
        store.finish_scan(scan_id, ScanStatus.COMPLETED, {"synthetic_files": count})
        stored = store.count_resources(scan_id)
        integrity = store.integrity_check()
    elapsed = time.perf_counter() - started
    current_python, peak_python = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    final_working_set, peak_working_set = _process_memory()
    if stored != count or not integrity:
        raise RuntimeError(
            "benchmark verification failed: "
            f"stored={stored}, expected={count}, integrity={integrity}"
        )
    return {
        "batch_size": batch_size,
        "count": count,
        "database_bytes": database.stat().st_size,
        "elapsed_seconds": round(elapsed, 3),
        "integrity": integrity,
        "peak_python_traced_bytes": peak_python,
        "final_python_traced_bytes": current_python,
        "baseline_working_set_bytes": baseline_working_set,
        "final_working_set_bytes": final_working_set,
        "peak_working_set_bytes": peak_working_set,
        "resources_per_second": round(count / elapsed, 1),
        "stored": stored,
    }


def _resource(index: int) -> Resource:
    return Resource(
        candidate_id=f"candidate_benchmark_{index:09d}",
        adapter_id="filesystem",
        display_name="Synthetic filesystem file",
        semantic_type=SemanticType.UNKNOWN,
        risk_tier=RiskTier.RED,
        provenance_class=ProvenanceClass.UNKNOWN,
        path=rf"G:\synthetic\file-{index:09d}.bin",
        logical_size=SizeValue(4096, Confidence.EXACT),
        allocated_size=SizeValue(4096, Confidence.EXACT),
        actionable=False,
    )


def _sha256(path: Path) -> str:
    before = path.stat(follow_symlinks=False)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat(follow_symlinks=False)
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or (before.st_ino and after.st_ino and before.st_ino != after.st_ino)
    ):
        raise RuntimeError("artifact changed while hashing")
    return digest.hexdigest()


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _process_memory() -> tuple[int | None, int | None]:
    if os.name != "nt":
        return (None, None)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    get_memory = psapi.GetProcessMemoryInfo
    get_memory.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
        wintypes.DWORD,
    )
    get_memory.restype = wintypes.BOOL
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    if not get_memory(
        kernel32.GetCurrentProcess(), ctypes.byref(counters), ctypes.sizeof(counters)
    ):
        return (None, None)
    return (int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))


if __name__ == "__main__":
    raise SystemExit(main())
