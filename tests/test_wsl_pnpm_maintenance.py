from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.wsl_pnpm_maintenance as wsl_pnpm
from devclean.core.wsl_exec import WslExecResult
from devclean.core.wsl_inventory import WslDistribution, WslInventory
from devclean.core.wsl_pnpm_maintenance import (
    WslPnpmStoreInventory,
    inventory_wsl_pnpm_store,
    prune_wsl_pnpm_store,
)


def _wsl_inventory(name: str = "Ubuntu", *, running: bool = True) -> WslInventory:
    return WslInventory(
        executable="wsl-test",
        version_text="WSL version: test",
        status_text="",
        distributions=(WslDistribution(name=name, running=running),),
    )


def _result(
    distribution: str,
    executable: str,
    arguments: tuple[str, ...],
    stdout: str = "",
    stderr: str = "",
) -> WslExecResult:
    return WslExecResult(
        distribution=distribution,
        executable=executable,
        arguments=arguments,
        command=("wsl-test", "--distribution", distribution, "--exec", executable, *arguments),
        stdout=stdout,
        stderr=stderr,
    )


def test_inventory_uses_pnpm_owned_version_and_active_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pnpm, "inspect_wsl", lambda environment=None: _wsl_inventory())
    seen: list[tuple[str, tuple[str, ...]]] = []

    def fake_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment, timeout
        seen.append((executable, arguments))
        if arguments == ("--version",):
            return _result(distribution, executable, arguments, "11.2.0\n")
        if arguments == ("store", "path", "--silent"):
            return _result(
                distribution,
                executable,
                arguments,
                "/home/me/.local/share/pnpm/store/v11\n",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)

    inventory = inventory_wsl_pnpm_store("ubuntu", {})

    assert inventory.distribution == "Ubuntu"
    assert inventory.version_text == "11.2.0"
    assert inventory.active_store_path == "/home/me/.local/share/pnpm/store/v11"
    assert inventory.store_dir == "/home/me/.local/share/pnpm/store"
    assert seen == [
        ("pnpm", ("--version",)),
        ("pnpm", ("store", "path", "--silent")),
    ]


def test_inventory_keeps_unversioned_store_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pnpm, "inspect_wsl", lambda environment=None: _wsl_inventory())

    def fake_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment, timeout
        output = "11.2.0\n" if arguments == ("--version",) else "/srv/pnpm-store\n"
        return _result(distribution, executable, arguments, output)

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)

    inventory = inventory_wsl_pnpm_store("Ubuntu", {})

    assert inventory.active_store_path == "/srv/pnpm-store"
    assert inventory.store_dir == "/srv/pnpm-store"


def test_inventory_rejects_non_absolute_or_root_store_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pnpm, "inspect_wsl", lambda environment=None: _wsl_inventory())

    for bad_path in ("relative/store\n", "/\n"):

        def fake_exec(
            distribution: str,
            executable: str,
            arguments: tuple[str, ...] = (),
            environment: Mapping[str, str] | None = None,
            *,
            timeout: int = 120,
            value: str = bad_path,
        ) -> WslExecResult:
            del environment, timeout
            output = "11.2.0\n" if arguments == ("--version",) else value
            return _result(distribution, executable, arguments, output)

        monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)
        with pytest.raises(RuntimeError, match="unsafe/non-absolute"):
            inventory_wsl_pnpm_store("Ubuntu", {})


def test_prune_confirms_scoped_store_and_uses_vendor_gc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.2.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )
    inventories = iter([inventory, inventory])
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: next(inventories),
    )
    seen: list[tuple[str, tuple[str, ...], int]] = []

    def fake_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment
        seen.append((executable, arguments, timeout))
        if executable == "ps":
            return _result(distribution, executable, arguments, "systemd /sbin/init\n")
        if arguments[-3:] == ("store", "path", "--silent"):
            return _result(
                distribution,
                executable,
                arguments,
                "/home/me/.local/share/pnpm/store/v11\n",
            )
        if arguments[-2:] == ("store", "prune"):
            return _result(distribution, executable, arguments, "Removed 20 files\n")
        raise AssertionError((executable, arguments))

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)

    result = prune_wsl_pnpm_store(inventory, {})

    assert ("ps", ("-ww", "-eo", "comm=,args="), 30) in seen
    assert (
        "pnpm",
        (
            "--store-dir",
            "/home/me/.local/share/pnpm/store",
            "store",
            "prune",
        ),
        900,
    ) in seen
    command_text = " ".join(result.command)
    assert " rm " not in f" {command_text} "
    assert result.output == "Removed 20 files"


@pytest.mark.parametrize(
    "process_line",
    [
        "pnpm pnpm install",
        "node node /usr/local/lib/node_modules/pnpm/bin/pnpm.cjs install",
        "nodejs nodejs /usr/lib/corepack/corepack.cjs pnpm install",
    ],
)
def test_prune_refuses_when_pnpm_process_is_running(
    monkeypatch: pytest.MonkeyPatch,
    process_line: str,
) -> None:
    inventory = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.2.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_pnpm,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            process_line + "\n",
        ),
    )

    with pytest.raises(RuntimeError, match="appears to be running"):
        prune_wsl_pnpm_store(inventory, {})


def test_prune_refuses_scoped_store_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.2.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: inventory,
    )

    def fake_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment, timeout
        if executable == "ps":
            return _result(distribution, executable, arguments, "systemd /sbin/init\n")
        return _result(distribution, executable, arguments, "/srv/other-store/v11\n")

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)

    with pytest.raises(RuntimeError, match="did not confirm"):
        prune_wsl_pnpm_store(inventory, {})


def test_prune_refuses_identity_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.2.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )
    changed = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.3.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: changed,
    )

    with pytest.raises(RuntimeError, match="changed before prune"):
        prune_wsl_pnpm_store(expected, {})
