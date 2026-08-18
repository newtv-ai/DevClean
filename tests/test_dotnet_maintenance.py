from __future__ import annotations

import subprocess

import pytest

import devclean.core.dotnet_maintenance as dotnet_maintenance
from devclean.core.dotnet_maintenance import run_dotnet_workload_clean


def test_dotnet_workload_clean_uses_conservative_vendor_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotnet_maintenance, "dotnet_sdk_process_running", lambda: False)
    env = {"DEVCLEAN_DOTNET_EXE": "dotnet-test"}

    def fake_run(
        command: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        encoding: str,
        errors: str,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert command == ["dotnet-test", "workload", "clean"]
        assert "--all" not in command
        assert check is False
        assert capture_output is True
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        assert timeout == 1_200
        assert env["DEVCLEAN_DOTNET_EXE"] == "dotnet-test"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Garbage collection completed.",
            stderr="",
        )

    result = run_dotnet_workload_clean(env, runner=fake_run)

    assert result.command == ("dotnet-test", "workload", "clean")
    assert result.returncode == 0
    assert result.output == "Garbage collection completed."


def test_dotnet_workload_clean_refuses_while_build_owner_is_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotnet_maintenance, "dotnet_sdk_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        run_dotnet_workload_clean({"DEVCLEAN_DOTNET_EXE": "dotnet-test"})


def test_dotnet_workload_clean_surfaces_vendor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotnet_maintenance, "dotnet_sdk_process_running", lambda: False)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["dotnet-test", "workload", "clean"],
            1,
            stdout="",
            stderr="workload state is locked",
        )

    with pytest.raises(RuntimeError, match="workload state is locked"):
        run_dotnet_workload_clean(
            {"DEVCLEAN_DOTNET_EXE": "dotnet-test"},
            runner=fake_run,
        )


def test_dotnet_workload_clean_combines_vendor_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dotnet_maintenance, "dotnet_sdk_process_running", lambda: False)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["dotnet-test", "workload", "clean"],
            0,
            stdout="cleaned orphaned packs",
            stderr="Visual Studio workloads require Visual Studio",
        )

    result = run_dotnet_workload_clean(
        {"DEVCLEAN_DOTNET_EXE": "dotnet-test"},
        runner=fake_run,
    )

    assert result.output == (
        "cleaned orphaned packs\nVisual Studio workloads require Visual Studio"
    )
