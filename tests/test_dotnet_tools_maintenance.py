from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from devclean.core import dotnet_tools_maintenance as maintenance


class SequenceRunner:
    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if not self.results:
            raise AssertionError("runner called more times than expected")
        return self.results.pop(0)


def completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def tool_list(*rows: str) -> str:
    return "\n".join(
        (
            "Package Id      Version      Commands",
            "---------------------------------------",
            *rows,
        )
    )


def test_default_global_tool_root_uses_userprofile(tmp_path: Path) -> None:
    root = maintenance.dotnet_global_tools_root({"USERPROFILE": str(tmp_path)})
    assert root == tmp_path / ".dotnet" / "tools"


def test_missing_userprofile_fails_closed_for_storage_root() -> None:
    assert maintenance.dotnet_global_tools_root({}) is None


def test_inventory_parses_vendor_list_and_total_storage(tmp_path: Path) -> None:
    root = tmp_path / ".dotnet" / "tools"
    store = root / ".store" / "dotnetsay" / "1.0.0"
    store.mkdir(parents=True)
    (store / "tool.dll").write_bytes(b"x" * 25)
    (root / "dotnetsay.exe").write_bytes(b"x" * 10)

    runner = SequenceRunner(
        completed(
            tool_list(
                "dotnetsay       1.0.0        dotnetsay",
                "dotnet-foo      2.4.1        dotnet-foo, foo-helper",
            )
        )
    )
    inventory = maintenance.inventory_dotnet_global_tools(
        {"USERPROFILE": str(tmp_path), "DEVCLEAN_DOTNET_EXE": "dotnet-test"},
        runner=runner,
    )

    assert runner.calls == [["dotnet-test", "tool", "list", "--global"]]
    assert inventory.storage_root == root
    assert inventory.logical_bytes == 35
    assert inventory.tools == (
        maintenance.DotnetGlobalTool("dotnetsay", "1.0.0", ("dotnetsay",)),
        maintenance.DotnetGlobalTool(
            "dotnet-foo", "2.4.1", ("dotnet-foo", "foo-helper")
        ),
    )


def test_inventory_accepts_empty_vendor_list(tmp_path: Path) -> None:
    runner = SequenceRunner(completed(tool_list()))
    inventory = maintenance.inventory_dotnet_global_tools(
        {"USERPROFILE": str(tmp_path)},
        runner=runner,
    )
    assert inventory.tools == ()
    assert inventory.logical_bytes == 0


def test_inventory_fails_closed_on_unparseable_output(tmp_path: Path) -> None:
    runner = SequenceRunner(completed("localized output without separator"))
    with pytest.raises(RuntimeError, match="unable to parse"):
        maintenance.inventory_dotnet_global_tools(
            {"USERPROFILE": str(tmp_path)},
            runner=runner,
        )


def test_inventory_surfaces_vendor_failure(tmp_path: Path) -> None:
    runner = SequenceRunner(completed(stderr="SDK missing", returncode=1))
    with pytest.raises(RuntimeError, match="SDK missing"):
        maintenance.inventory_dotnet_global_tools(
            {"USERPROFILE": str(tmp_path)},
            runner=runner,
        )


def test_uninstall_requires_currently_listed_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        maintenance.dotnet_maintenance, "dotnet_sdk_process_running", lambda: False
    )
    runner = SequenceRunner(completed(tool_list("dotnetsay 1.0.0 dotnetsay")))

    with pytest.raises(ValueError, match="not an installed"):
        maintenance.uninstall_dotnet_global_tool(
            "other-tool",
            {"USERPROFILE": str(tmp_path)},
            runner=runner,
        )
    assert len(runner.calls) == 1


def test_uninstall_delegates_exact_package_to_vendor_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        maintenance.dotnet_maintenance, "dotnet_sdk_process_running", lambda: False
    )
    root = tmp_path / ".dotnet" / "tools"
    store = root / ".store" / "dotnetsay" / "1.0.0"
    store.mkdir(parents=True)
    payload = store / "tool.dll"
    payload.write_bytes(b"x" * 40)

    def uninstall_result(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [
            "dotnet-test",
            "tool",
            "uninstall",
            "--global",
            "dotnetsay",
        ]
        payload.unlink()
        return completed(stdout="Tool 'dotnetsay' was successfully uninstalled.")

    class Runner:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self, command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            self.calls += 1
            if self.calls == 1:
                assert command == ["dotnet-test", "tool", "list", "--global"]
                return completed(tool_list("dotnetsay 1.0.0 dotnetsay"))
            return uninstall_result(command, **kwargs)

    result = maintenance.uninstall_dotnet_global_tool(
        "DOTNETSAY",
        {"USERPROFILE": str(tmp_path), "DEVCLEAN_DOTNET_EXE": "dotnet-test"},
        runner=Runner(),
    )

    assert result.tool.package_id == "dotnetsay"
    assert result.before_bytes == 40
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 40
    assert result.command == (
        "dotnet-test",
        "tool",
        "uninstall",
        "--global",
        "dotnetsay",
    )


def test_uninstall_refuses_while_dotnet_owner_is_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        maintenance.dotnet_maintenance, "dotnet_sdk_process_running", lambda: True
    )
    runner = SequenceRunner(completed(tool_list("dotnetsay 1.0.0 dotnetsay")))

    with pytest.raises(RuntimeError, match="is running"):
        maintenance.uninstall_dotnet_global_tool(
            "dotnetsay",
            {"USERPROFILE": str(tmp_path)},
            runner=runner,
        )
    assert len(runner.calls) == 1


def test_uninstall_surfaces_vendor_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        maintenance.dotnet_maintenance, "dotnet_sdk_process_running", lambda: False
    )
    runner = SequenceRunner(
        completed(tool_list("dotnetsay 1.0.0 dotnetsay")),
        completed(stderr="uninstall failed", returncode=2),
    )

    with pytest.raises(RuntimeError, match="uninstall failed"):
        maintenance.uninstall_dotnet_global_tool(
            "dotnetsay",
            {"USERPROFILE": str(tmp_path)},
            runner=runner,
        )
    assert len(runner.calls) == 2
