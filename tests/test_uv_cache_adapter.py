from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reclaimer.adapters.base import AdapterContext, ProbeStatus
from reclaimer.adapters.uv_cache import UvCacheAdapter, parse_cache_size
from reclaimer.core.models import Confidence, SemanticType
from reclaimer.evidence.store import EvidenceStore
from reclaimer.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)


@pytest.mark.parametrize("text", ["0", "7\n", "10203294578\r\n"])
def test_uv_cache_size_accepts_one_integer(text: str) -> None:
    assert parse_cache_size(text) == int(text.strip())


@pytest.mark.parametrize(
    "text",
    ["", "-1\n", "1.5\n", "1 MiB\n", "1\n2\n", " 1\n", "1\x1b[0m\n"],
)
def test_uv_cache_size_rejects_non_contract_output(text: str) -> None:
    with pytest.raises(ValueError):
        parse_cache_size(text)


def test_uv_adapter_measures_cache_and_never_constructs_maintenance(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "uv-cache"
    cache_root.mkdir()
    (cache_root / "artifact.bin").write_bytes(b"1234567")
    responses = iter(
        (
            b"uv 0.11.6 (fixture)\n",
            b"Commands: clean prune dir size\n",
            b"Usage: uv cache size [OPTIONS]\n",
            f"{cache_root}\r\n".encode(),
            b"7\n",
        )
    )
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _result(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = UvCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 1
    resource = result.resources[0]
    assert resource.semantic_type is SemanticType.REBUILDABLE_CACHE
    assert resource.logical_size.value == 7
    assert resource.logical_size.confidence is Confidence.ESTIMATE
    assert resource.allocated_size.value is not None
    assert resource.actionable is False
    assert len(result.evidence) == 5
    assert all(
        "clean" not in argv and "prune" not in argv and "--force" not in argv
        for argv in commands
    )
    assert commands[-1][1:6] == (
        "--preview-features",
        "cache-size",
        "cache",
        "size",
        "--cache-dir",
    )


def test_uv_adapter_rejects_old_version_before_cache_queries(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _result(command.argv, b"uv 0.9.7\n")

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = UvCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.UNSUPPORTED_VERSION
    assert len(commands) == 1
    assert result.resources == ()


def test_uv_size_timeout_discards_partial_candidate(tmp_path: Path) -> None:
    cache_root = tmp_path / "uv-cache"
    cache_root.mkdir()
    responses = iter(
        (
            _result((str(sys.executable),), b"uv 0.11.6\n"),
            _result((str(sys.executable),), b"dir size\n"),
            _result((str(sys.executable),), b"size help\n"),
            _result((str(sys.executable),), f"{cache_root}\n".encode()),
            BoundedProcessResult(
                argv=(str(sys.executable),),
                returncode=1,
                stdout=b"7",
                stderr=b"",
                duration_ms=120000,
                termination=ProcessTermination.TIMED_OUT,
            ),
        )
    )

    def runner(command) -> BoundedProcessResult:
        result = next(responses)
        return BoundedProcessResult(
            argv=command.argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            termination=result.termination,
        )

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = UvCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.ERROR
    assert result.resources == ()
    assert result.issues[0].code == "CACHE_SIZE_FAILED"


def _result(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
