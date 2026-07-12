from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import reclaimer.adapters.command as command_module
from reclaimer.adapters.command import QueryCommand, build_query_environment, run_query
from reclaimer.core.models import EffectClass
from reclaimer.platform.windows.process import ProcessLimits


def test_query_command_rejects_execution_effects() -> None:
    with pytest.raises(ValueError, match="maintenance or destructive"):
        QueryCommand(
            adapter_id="fixture",
            executable=Path(sys.executable),
            arguments=("--version",),
            effect_class=EffectClass.DESTRUCTIVE,
        )


@pytest.mark.parametrize("name", ("query.cmd", "query.bat", "query.CMD", "query"))
def test_query_command_rejects_non_executable_extensions(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"\.exe"):
        QueryCommand(
            adapter_id="fixture",
            executable=tmp_path / name,
            arguments=("--version",),
            effect_class=EffectClass.PURE_QUERY,
        )


def test_query_environment_excludes_proxies_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@proxy.example")
    monkeypatch.setenv("HF_TOKEN", "secret-token")
    monkeypatch.setenv("PIP_CACHE_DIR", r"C:\cache\pip")

    environment = build_query_environment(
        Path(sys.executable), {"PIP_NO_INDEX": "1"}
    )

    assert "HTTPS_PROXY" not in environment
    assert "HF_TOKEN" not in environment
    assert environment["PIP_CACHE_DIR"] == r"C:\cache\pip"
    assert environment["PIP_NO_INDEX"] == "1"
    assert Path(sys.executable).parent == Path(environment["PATH"].split(os.pathsep)[0])


def test_query_environment_rejects_unregistered_override() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        build_query_environment(Path(sys.executable), {"HTTP_PROXY": "http://proxy"})


def test_run_query_fails_closed_when_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_module, "is_process_elevated", lambda: True)
    query = QueryCommand(
        adapter_id="fixture",
        executable=Path(sys.executable),
        arguments=("--version",),
        effect_class=EffectClass.PURE_QUERY,
    )

    with pytest.raises(PermissionError, match="elevated"):
        run_query(query)


def test_run_query_uses_bounded_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_module, "is_process_elevated", lambda: False)
    query = QueryCommand(
        adapter_id="fixture",
        executable=Path(sys.executable),
        arguments=("-c", "print('fixture')"),
        effect_class=EffectClass.PURE_QUERY,
        limits=ProcessLimits(timeout_seconds=2, max_stdout_bytes=128),
        environment_overrides=(("PIP_NO_INDEX", "1"),),
    )

    result = run_query(query)

    assert result.succeeded
    assert result.stdout.strip() == b"fixture"


def test_query_command_rejects_duplicate_or_sensitive_environment_keys() -> None:
    with pytest.raises(ValueError, match="unique"):
        QueryCommand(
            adapter_id="fixture",
            executable=Path(sys.executable),
            arguments=("--version",),
            effect_class=EffectClass.PURE_QUERY,
            environment_overrides=(("PIP_NO_INDEX", "1"), ("PIP_NO_INDEX", "1")),
        )
    with pytest.raises(ValueError, match="unsupported"):
        QueryCommand(
            adapter_id="fixture",
            executable=Path(sys.executable),
            arguments=("--version",),
            effect_class=EffectClass.PURE_QUERY,
            environment_overrides=(("HF_TOKEN", "secret"),),
        )
