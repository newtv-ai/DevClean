"""Suite-wide isolation: tests never start real programs or touch real user data.

Root discovery asks installed package managers (npm, pnpm, go, uv, ...) where
their caches live. Some tests set Windows-style locations such as
``NPM_CONFIG_CACHE=E:\\npm-cache``; a real npm on the test machine would then
write logs there. Every external program start is refused as if the tool were
not installed. The test's own interpreter stays allowed because some tests run
a tiny fake vendor server with it.

The pre-delete check of running programs and services reads the host's real
process list and registry on Windows. Tests start from "nothing is running";
a test that is about running programs or services replaces these lists itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from devclean.core import live_owners
from devclean.core.application_cleanup import clear_process_cache


def _is_test_interpreter(program: object) -> bool:
    if isinstance(program, os.PathLike):
        program = os.fspath(program)
    if not isinstance(program, str):
        return False
    interpreter = os.path.abspath(sys.executable)
    return os.path.normcase(os.path.abspath(program)) == os.path.normcase(interpreter)


class _NoExternalProgramPopen(subprocess.Popen[Any]):
    def __init__(self, args: Any, *more: Any, **kwargs: Any) -> None:
        argv = args if isinstance(args, (list, tuple)) else [args]
        if not argv or not _is_test_interpreter(argv[0]):
            self._child_created = False  # keeps Popen.__del__ quiet
            raise FileNotFoundError(f"tests may not start external programs: {argv[:1]}")
        super().__init__(args, *more, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_from_programs_and_user_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    # Cleared before each test only: at teardown a test's own monkeypatches of
    # these cached functions may still be in place.
    clear_process_cache()
    monkeypatch.setattr(subprocess, "Popen", _NoExternalProgramPopen)
    monkeypatch.setattr(live_owners, "list_processes", lambda: ())
    monkeypatch.setattr(live_owners, "list_services", lambda: ())
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    yield
