from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.wsl_go_mod_cache as wsl_go
from devclean.core.wsl_exec import WslExecResult
from devclean.core.wsl_go_mod_cache import (
    WslGoModCacheInventory,
    clean_wsl_go_mod_cache,
    inventory_wsl_go_mod_cache,
)
from devclean.core.wsl_inventory import WslDistribution, WslInventory


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


def test_inventory_uses_go_owned_structured_modcache_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_go, "inspect_wsl", lambda environment=None: _wsl_inventory())
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
        if arguments == ("version",):
            return _result(
                distribution,
                executable,
                arguments,
                "go version go1.26.5 linux/amd64\n",
            )
        if arguments == ("env", "-json", "GOMODCACHE"):
            return _result(
                distribution,
                executable,
                arguments,
                '{"GOMODCACHE":"/home/me/go/pkg/mod"}\n',
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(wsl_go, "run_wsl_exec", fake_exec)
    inventory = inventory_wsl_go_mod_cache("ubuntu", {})
    assert inventory.distribution == "Ubuntu"
    assert inventory.module_cache_path == "/home/me/go/pkg/mod"
    assert seen == [
        ("go", ("version",)),
        ("go", ("env", "-json", "GOMODCACHE")),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        '{"GOMODCACHE":"relative/mod"}',
        '{"GOMODCACHE":"/"}',
        '{"GOMODCACHE":42}',
        "not-json",
    ],
)
def test_inventory_rejects_unsafe_or_invalid_modcache(
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    monkeypatch.setattr(wsl_go, "inspect_wsl", lambda environment=None: _wsl_inventory())

    def fake_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment, timeout
        output = "go version go1.26.5 linux/amd64" if arguments == ("version",) else payload
        return _result(distribution, executable, arguments, output)

    monkeypatch.setattr(wsl_go, "run_wsl_exec", fake_exec)
    with pytest.raises(RuntimeError):
        inventory_wsl_go_mod_cache("Ubuntu", {})


def test_clean_revalidates_scope_idle_and_pins_all_clean_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.5 linux/amd64",
        "/home/me/go/pkg/mod",
    )
    inventories = iter([inventory, inventory])
    monkeypatch.setattr(
        wsl_go,
        "inventory_wsl_go_mod_cache",
        lambda distribution, environment=None: next(inventories),
    )
    scope_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wsl_go,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: scope_calls.append((distribution, path)),
    )
    monkeypatch.setattr(
        wsl_go,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            "systemd /sbin/init\n",
        ),
    )
    env_calls: list[tuple[str, tuple[str, ...], dict[str, str], int]] = []

    def fake_env_exec(
        distribution: str,
        executable: str,
        arguments: tuple[str, ...] = (),
        linux_environment: Mapping[str, str] | None = None,
        environment: Mapping[str, str] | None = None,
        *,
        timeout: int = 120,
    ) -> WslExecResult:
        del environment
        env_calls.append(
            (distribution, arguments, dict(linux_environment or {}), timeout)
        )
        return _result(distribution, executable, arguments)

    monkeypatch.setattr(wsl_go, "run_wsl_exec_with_env", fake_env_exec)
    clean_wsl_go_mod_cache(inventory, {})

    assert scope_calls == [("Ubuntu", "/home/me/go/pkg/mod")]
    assert env_calls == [
        (
            "Ubuntu",
            (
                "clean",
                "-i=false",
                "-r=false",
                "-cache=false",
                "-testcache=false",
                "-modcache=true",
                "-fuzzcache=false",
            ),
            {"GOMODCACHE": "/home/me/go/pkg/mod"},
            1800,
        )
    ]


@pytest.mark.parametrize(
    "process_line",
    [
        "go go mod download",
        "gopls /usr/local/bin/gopls serve",
    ],
)
def test_clean_refuses_when_go_or_gopls_is_running(
    monkeypatch: pytest.MonkeyPatch,
    process_line: str,
) -> None:
    inventory = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.5 linux/amd64",
        "/home/me/go/pkg/mod",
    )
    monkeypatch.setattr(
        wsl_go,
        "inventory_wsl_go_mod_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_go,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            process_line + "\n",
        ),
    )
    with pytest.raises(RuntimeError, match="appears to be running"):
        clean_wsl_go_mod_cache(inventory, {})


def test_clean_fails_closed_when_process_state_cannot_be_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.5 linux/amd64",
        "/home/me/go/pkg/mod",
    )
    monkeypatch.setattr(
        wsl_go,
        "inventory_wsl_go_mod_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_go,
        "run_wsl_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ps failed")),
    )
    with pytest.raises(RuntimeError, match="process state"):
        clean_wsl_go_mod_cache(inventory, {})


def test_clean_refuses_non_root_filesystem_before_vendor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.5 linux/amd64",
        "/mnt/c/go/pkg/mod",
    )
    monkeypatch.setattr(
        wsl_go,
        "inventory_wsl_go_mod_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_go,
        "_require_go_idle",
        lambda distribution, environment=None: None,
    )
    monkeypatch.setattr(
        wsl_go,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: (_ for _ in ()).throw(
            RuntimeError("mounted filesystem")
        ),
    )
    called = False

    def unexpected(*args: object, **kwargs: object) -> WslExecResult:
        nonlocal called
        called = True
        raise AssertionError("vendor mutation should not run")

    monkeypatch.setattr(wsl_go, "run_wsl_exec_with_env", unexpected)
    with pytest.raises(RuntimeError, match="mounted filesystem"):
        clean_wsl_go_mod_cache(inventory, {})
    assert not called


def test_clean_refuses_identity_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.5 linux/amd64",
        "/home/me/go/pkg/mod",
    )
    changed = WslGoModCacheInventory(
        "Ubuntu",
        True,
        "go version go1.26.6 linux/amd64",
        "/home/me/go/pkg/mod",
    )
    monkeypatch.setattr(
        wsl_go,
        "inventory_wsl_go_mod_cache",
        lambda distribution, environment=None: changed,
    )
    with pytest.raises(RuntimeError, match="changed before clean"):
        clean_wsl_go_mod_cache(expected, {})
