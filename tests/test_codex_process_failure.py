from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from devclean.core import _application_cleanup_impl as impl
from devclean.core import application_cleanup


@pytest.mark.parametrize(
    ("returncode", "stdout", "blocked"),
    [
        (1, "", True),
        (1, '"explorer.exe","123","Console","1","10 K"', True),
        (0, "", True),
        (0, "unexpected output", True),
        (0, '"codex.exe","123","Console","1","10 K"', True),
        (0, '"ChatGPT.exe","123","Console","1","10 K"', True),
        (0, '"explorer.exe","123","Console","1","10 K"', False),
    ],
)
def test_codex_guard_requires_successful_parseable_process_inventory(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    blocked: bool,
) -> None:
    monkeypatch.setattr(impl, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode, stdout, "Access denied"
        ),
    )
    impl.clear_process_cache()
    try:
        assert application_cleanup.application_process_running("codex") is blocked
    finally:
        impl.clear_process_cache()
