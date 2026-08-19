from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.wsl_uv_maintenance as wsl_uv
from devclean.core.wsl_exec import WslExecResult
from devclean.core.wsl_inventory import WslDistribution, WslInventory
from devclean.core.wsl_uv_maintenance import (
    WslUvCacheInventory,
    inventory_wsl_uv_cache,
    prune_wsl_uv_cache,
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


def test_inventory_uses_uv_owned_version_and_cache_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_uv, "inspect_wsl", lambda environment=None: _wsl_inventory())
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
            return _result(distribution, executable, arguments, "uv 0.9.0\n")
        if arguments == ("cache", "dir"):
            return _result(distribution, executable, arguments, "/home/me/.cache/uv\n")
        raise AssertionError(arguments)

    monkeypatch.setattr(wsl_uv, "run_wsl_exec", fake_exec)

    inventory = inventory_wsl_uv_cache("ubuntu", {})

    assert inventory.distribution == "Ubuntu"
    assert inventory.version_text == "uv 0.9.0"
    assert inventory.cache_path == "/home/me/.cache/uv"
    assert seen == [
        ("uv", ("--version",)),
        ("uv", ("cache", "dir")),
    ]


def test_inventory_rejects_non_absolute_or_root_cache_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_uv, "inspect_wsl", lambda environment=None: _wsl_inventory())

    for bad_path in ("relative/cache\n", "/\n"):

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
            if arguments == ("--version",):
                return _result(distribution, executable, arguments, "uv 0.9.0\n")
            return _result(distribution, executable, arguments, value)

        monkeypatch.setattr(wsl_uv, "run_wsl_exec", fake_exec)
        with pytest.raises(RuntimeError, match="unsafe/non-absolute"):
            inventory_wsl_uv_cache("Ubuntu", {})


def test_prune_pins_exact_rootfs_cache_and_uses_no_force_or_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter(
        [
            WslUvCacheInventory("Ubuntu", True, "uv 0.9.0", "/home/me/.cache/uv"),
            WslUvCacheInventory("Ubuntu", True, "uv 0.9.0", "/home/me/.cache/uv"),
        ]
    )
    monkeypatch.setattr(
        wsl_uv,
        "inventory_wsl_uv_cache",
        lambda distribution, environment=None: next(inventories),
    )
    scope_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wsl_uv,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: scope_calls.append((distribution, path)),
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
        return _result(
            distribution,
            executable,
            arguments,
            "Removed 42 files (12.0MiB)\n",
        )

    monkeypatch.setattr(wsl_uv, "run_wsl_exec", fake_exec)
    expected = WslUvCacheInventory(
        "Ubuntu",
        False,
        "uv 0.9.0",
        "/home/me/.cache/uv",
    )

    result = prune_wsl_uv_cache(expected, {})

    assert scope_calls == [("Ubuntu", "/home/me/.cache/uv")]
    assert seen == [
        (
            "uv",
            ("--cache-dir", "/home/me/.cache/uv", "cache", "prune"),
            900,
        )
    ]
    command_text = " ".join(result.command)
    assert "--force" not in command_text
    assert "--ci" not in command_text
    assert "cache clean" not in command_text
    assert result.output.startswith("Removed 42 files")


def test_prune_refuses_non_root_filesystem_before_vendor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslUvCacheInventory(
        "Ubuntu",
        True,
        "uv 0.9.0",
        "/mnt/share/uv-cache",
    )
    monkeypatch.setattr(
        wsl_uv,
        "inventory_wsl_uv_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_uv,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: (_ for _ in ()).throw(
            RuntimeError("mounted filesystem")
        ),
    )
    called = False

    def unexpected_exec(*args: object, **kwargs: object) -> WslExecResult:
        nonlocal called
        called = True
        raise AssertionError("vendor mutation should not run")

    monkeypatch.setattr(wsl_uv, "run_wsl_exec", unexpected_exec)

    with pytest.raises(RuntimeError, match="mounted filesystem"):
        prune_wsl_uv_cache(inventory, {})
    assert not called


def test_prune_refuses_identity_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = WslUvCacheInventory(
        "Ubuntu",
        True,
        "uv 0.9.0",
        "/home/me/.cache/uv",
    )
    changed = WslUvCacheInventory(
        "Ubuntu",
        True,
        "uv 0.9.1",
        "/home/me/.cache/uv",
    )
    monkeypatch.setattr(
        wsl_uv,
        "inventory_wsl_uv_cache",
        lambda distribution, environment=None: changed,
    )

    with pytest.raises(RuntimeError, match="changed before prune"):
        prune_wsl_uv_cache(expected, {})


def test_prune_refuses_cache_change_after_vendor_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = WslUvCacheInventory(
        "Ubuntu",
        True,
        "uv 0.9.0",
        "/home/me/.cache/uv",
    )
    changed_after = WslUvCacheInventory(
        "Ubuntu",
        True,
        "uv 0.9.0",
        "/mnt/c/other-uv-cache",
    )
    inventories = iter([before, changed_after])
    monkeypatch.setattr(
        wsl_uv,
        "inventory_wsl_uv_cache",
        lambda distribution, environment=None: next(inventories),
    )
    monkeypatch.setattr(
        wsl_uv,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: None,
    )
    monkeypatch.setattr(
        wsl_uv,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            "Removed 1 file\n",
        ),
    )

    with pytest.raises(RuntimeError, match="changed after prune"):
        prune_wsl_uv_cache(before, {})
