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
) -> WslExecResult:
    return WslExecResult(
        distribution=distribution,
        executable=executable,
        arguments=arguments,
        command=("wsl-test", "--distribution", distribution, "--exec", executable, *arguments),
        stdout=stdout,
        stderr="",
    )


def _inventory() -> WslPnpmStoreInventory:
    return WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.2.0",
        "/home/me/.local/share/pnpm/store/v11",
        "/home/me/.local/share/pnpm/store",
    )


def test_inventory_uses_vendor_active_store_and_derives_store_dir(
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
        if arguments == ("--version",):
            return _result(distribution, executable, arguments, "11.2.0\n")
        return _result(
            distribution,
            executable,
            arguments,
            "/home/me/.local/share/pnpm/store/v11\n",
        )

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)
    inventory = inventory_wsl_pnpm_store("ubuntu", {})
    assert inventory.distribution == "Ubuntu"
    assert inventory.active_store_path.endswith("/store/v11")
    assert inventory.store_dir.endswith("/store")


def test_inventory_rejects_relative_or_root_store_path(
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


def test_prune_requires_rootfs_for_active_store_and_store_dir_before_gc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory()
    inventories = iter([inventory, inventory])
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: next(inventories),
    )
    scope_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wsl_pnpm,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: scope_calls.append((distribution, path)),
    )
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
        if executable == "ps":
            return _result(distribution, executable, arguments, "systemd /sbin/init\n")
        if arguments[-3:] == ("store", "path", "--silent"):
            return _result(distribution, executable, arguments, inventory.active_store_path + "\n")
        if arguments[-2:] == ("store", "prune"):
            return _result(distribution, executable, arguments, "Removed 20 files\n")
        raise AssertionError((executable, arguments))

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", fake_exec)
    result = prune_wsl_pnpm_store(inventory, {})
    assert scope_calls == [
        ("Ubuntu", inventory.active_store_path),
        ("Ubuntu", inventory.store_dir),
    ]
    assert ("pnpm", ("--store-dir", inventory.store_dir, "store", "prune")) in seen
    assert result.output == "Removed 20 files"


def test_prune_refuses_cross_mount_before_pnpm_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _inventory()
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(wsl_pnpm, "_require_pnpm_idle", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        wsl_pnpm,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: (_ for _ in ()).throw(
            RuntimeError("mounted filesystem")
        ),
    )
    called = False

    def unexpected_exec(*args: object, **kwargs: object) -> WslExecResult:
        nonlocal called
        called = True
        raise AssertionError("pnpm mutation should not run")

    monkeypatch.setattr(wsl_pnpm, "run_wsl_exec", unexpected_exec)
    with pytest.raises(RuntimeError, match="mounted filesystem"):
        prune_wsl_pnpm_store(inventory, {})
    assert not called


@pytest.mark.parametrize(
    "process_line",
    [
        "pnpm pnpm install",
        "node node /usr/local/lib/node_modules/pnpm/bin/pnpm.cjs install",
    ],
)
def test_prune_refuses_running_pnpm(
    monkeypatch: pytest.MonkeyPatch,
    process_line: str,
) -> None:
    inventory = _inventory()
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


def test_prune_refuses_identity_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _inventory()
    changed = WslPnpmStoreInventory(
        "Ubuntu",
        True,
        "11.3.0",
        expected.active_store_path,
        expected.store_dir,
    )
    monkeypatch.setattr(
        wsl_pnpm,
        "inventory_wsl_pnpm_store",
        lambda distribution, environment=None: changed,
    )
    with pytest.raises(RuntimeError, match="changed before prune"):
        prune_wsl_pnpm_store(expected, {})
