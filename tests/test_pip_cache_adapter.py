from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reclaimer.adapters.base import AdapterContext, ProbeStatus
from reclaimer.adapters.pip_cache import PipCacheAdapter, parse_cache_list
from reclaimer.core.models import Confidence, SemanticType
from reclaimer.evidence.store import EvidenceStore
from reclaimer.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)


def test_parse_pip_cache_list_accepts_empty_and_contained_paths(tmp_path: Path) -> None:
    wheel = tmp_path / "wheels" / "demo-1.0-py3-none-any.whl"

    assert parse_cache_list("", tmp_path) == ()
    assert parse_cache_list("No locally built wheels cached.\r\n", tmp_path) == ()
    assert parse_cache_list(f"{wheel}\r\n", tmp_path) == (wheel,)


@pytest.mark.parametrize(
    "text",
    [
        r"G:\outside\private.whl",
        "relative.whl",
        "bad\x1b[31m.whl",
        "bad\u202efile.whl",
    ],
)
def test_parse_pip_cache_list_rejects_unsafe_paths(tmp_path: Path, text: str) -> None:
    with pytest.raises(ValueError):
        parse_cache_list(text, tmp_path)


def test_pip_adapter_measures_root_without_constructing_cleanup_commands(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "pip-cache"
    wheel = cache_root / "wheels" / "demo-1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"fixture-wheel")
    responses = iter(
        (
            b"pip 26.1.1 from G:\\fixture\\pip (python 3.13)\n",
            b"Commands: dir info list remove purge\n",
            f"{cache_root}\r\n".encode(),
            b"Package index page cache size: 0 bytes\n",
            f"{wheel}\r\n".encode(),
        )
    )
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )

    result = PipCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 1
    resource = result.resources[0]
    assert resource.semantic_type is SemanticType.REBUILDABLE_CACHE
    assert resource.logical_size.value == len(b"fixture-wheel")
    assert resource.logical_size.confidence is Confidence.EXACT
    assert resource.actionable is False
    assert len(result.evidence) == 5
    assert all("purge" not in argv and "remove" not in argv for argv in commands)
    assert commands[-1][-3:] == ("cache", "list", "--format=abspath")


def test_pip_adapter_rejects_pre_21_without_cache_queries(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, b"pip 20.3.4 from G:\\fixture (python 3.8)\n")

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )

    result = PipCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.UNSUPPORTED_VERSION
    assert len(commands) == 1
    assert result.resources == ()


def test_pip_adapter_fails_closed_on_invalid_utf8(tmp_path: Path) -> None:
    responses = iter((b"pip 26.1.1 from fixture\n", b"\xff"))

    def runner(command) -> BoundedProcessResult:
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = PipCacheAdapter(Path(sys.executable)).inventory(context)

    assert result.probe.status is ProbeStatus.ERROR
    assert result.issues[0].fatal is True
    assert result.resources == ()


def _success(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
