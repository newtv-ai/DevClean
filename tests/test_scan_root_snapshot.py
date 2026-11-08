"""A scan discovers application roots once, not once per scanned file."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

import devclean.core.visual_studio_installer_cleanup as visual_studio_installer_cleanup
from devclean.core.application_cleanup import (
    clear_process_cache,
    match_application_rule,
    scan_root_snapshot,
)
from devclean.core.user_rules import default_rules
from devclean.core.visual_studio_installer_cleanup import visual_studio_installer_roots
from devclean.platform.windows.registry import RegistryStringLookup
from devclean.scanner import CancellationToken
from devclean.ui import product_app

# Paths no application owns, so every profile's matcher is consulted.
_UNOWNED = tuple(rf"Z:\Downloads\OldApp-Setup-1.{index}.zip" for index in range(25))


class _NoProcess:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("tests never start real tools")


@pytest.fixture
def registry_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Count Visual Studio Installer CachePath reads: one per root discovery."""

    reads: list[str] = []

    def lookup(_keys: Iterable[str], value_name: str) -> RegistryStringLookup:
        reads.append(value_name)
        return RegistryStringLookup(None, True)

    monkeypatch.setattr(visual_studio_installer_cleanup, "first_hklm_string_value", lookup)
    monkeypatch.setattr(subprocess, "Popen", _NoProcess)
    home = tmp_path / "home"
    for name, value in {
        "USERPROFILE": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "TEMP": home / "AppData" / "Local" / "Temp",
        "TMP": home / "AppData" / "Local" / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
    }.items():
        monkeypatch.setenv(name, str(value))
    clear_process_cache()
    yield reads
    clear_process_cache()


def test_scan_snapshot_discovers_application_roots_once(registry_reads: list[str]) -> None:
    with scan_root_snapshot():
        for path in _UNOWNED:
            assert match_application_rule(path) is None

    assert len(registry_reads) == 1


def test_root_discovery_outside_a_scan_is_never_reused(registry_reads: list[str]) -> None:
    with scan_root_snapshot():
        match_application_rule(_UNOWNED[0])
    for path in _UNOWNED[1:4]:
        match_application_rule(path)

    assert len(registry_reads) == 4


def test_explicit_environment_is_never_served_from_the_snapshot(
    tmp_path: Path,
    registry_reads: list[str],
) -> None:
    with scan_root_snapshot():
        first = visual_studio_installer_roots({"PROGRAMDATA": str(tmp_path / "a")})
        second = visual_studio_installer_roots({"PROGRAMDATA": str(tmp_path / "b")})

    assert first != second
    assert len(registry_reads) == 2


def test_product_scan_discovers_application_roots_once_for_all_files(
    tmp_path: Path,
    registry_reads: list[str],
) -> None:
    root = tmp_path / "scan"
    root.mkdir()
    for index in range(20):
        (root / f"file{index}.txt").write_bytes(b"x")
    window = object.__new__(product_app.ProductDevCleanWindow)
    events: Queue[tuple[str, Any]] = Queue()
    window._events = events

    window._scan_worker("scan", (root,), CancellationToken(), default_rules(), ())

    kinds = []
    while not events.empty():
        kinds.append(events.get_nowait()[0])
    assert "scan_done" in kinds
    assert len(registry_reads) == 1
