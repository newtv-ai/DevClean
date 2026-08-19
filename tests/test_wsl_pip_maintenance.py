from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.wsl_pip_maintenance as wsl_pip
from devclean.core.wsl_exec import WslExecResult
from devclean.core.wsl_inventory import WslDistribution, WslInventory
from devclean.core.wsl_pip_maintenance import (
    WslPipCacheInventory,
    WslPipEntrypoint,
    inventory_wsl_pip_cache,
    purge_wsl_pip_cache,
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


def test_inventory_prefers_python_module_and_uses_pip_owned_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pip, "inspect_wsl", lambda environment=None: _wsl_inventory())
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
        if arguments == ("-m", "pip", "cache", "dir"):
            return _result(distribution, executable, arguments, "/home/me/.cache/pip\n")
        if arguments == ("-m", "pip", "--version"):
            return _result(distribution, executable, arguments, "pip 26.2 from /opt/pip\n")
        if arguments == ("-m", "pip", "cache", "info"):
            return _result(
                distribution,
                executable,
                arguments,
                "Package index page cache size: 42 MB\n",
            )
        raise AssertionError(arguments)

    monkeypatch.setattr(wsl_pip, "run_wsl_exec", fake_exec)

    inventory = inventory_wsl_pip_cache("ubuntu", {})

    assert inventory.distribution == "Ubuntu"
    assert inventory.cache_path == "/home/me/.cache/pip"
    assert inventory.entrypoint.display == "python3 -m pip"
    assert inventory.entrypoint.version_text.startswith("pip 26.2")
    assert seen == [
        ("python3", ("-m", "pip", "cache", "dir")),
        ("python3", ("-m", "pip", "--version")),
        ("python3", ("-m", "pip", "cache", "info")),
    ]


def test_inventory_falls_back_to_direct_pip_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pip, "inspect_wsl", lambda environment=None: _wsl_inventory())
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
        if executable in {"python3", "python"}:
            raise RuntimeError("missing")
        if executable == "pip3" and arguments == ("cache", "dir"):
            return _result(distribution, executable, arguments, "/home/me/.cache/pip\n")
        if executable == "pip3" and arguments == ("--version",):
            return _result(distribution, executable, arguments, "pip 25.0 from /usr/lib/pip\n")
        if executable == "pip3" and arguments == ("cache", "info"):
            return _result(distribution, executable, arguments, "cache info\n")
        raise AssertionError((executable, arguments))

    monkeypatch.setattr(wsl_pip, "run_wsl_exec", fake_exec)

    inventory = inventory_wsl_pip_cache("Ubuntu", {})

    assert inventory.entrypoint.executable == "pip3"
    assert all(executable not in {"sh", "bash"} for executable, _arguments in seen)


def test_inventory_rejects_non_absolute_or_root_cache_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_pip, "inspect_wsl", lambda environment=None: _wsl_inventory())

    for bad_path in ("relative/cache\n", "/\n"):

        def fake_bad_path(
            distribution: str,
            executable: str,
            arguments: tuple[str, ...] = (),
            environment: Mapping[str, str] | None = None,
            *,
            timeout: int = 120,
            value: str = bad_path,
        ) -> WslExecResult:
            del environment, timeout
            return _result(distribution, executable, arguments, value)

        monkeypatch.setattr(wsl_pip, "run_wsl_exec", fake_bad_path)
        with pytest.raises(RuntimeError, match="unsafe/non-absolute"):
            inventory_wsl_pip_cache("Ubuntu", {})


def test_purge_revalidates_identity_checks_scope_processes_and_uses_cache_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = WslPipEntrypoint("python3", ("-m", "pip"), "pip 26.2 from /opt/pip")
    inventories = iter(
        [
            WslPipCacheInventory("Ubuntu", True, entrypoint, "/home/me/.cache/pip", "before"),
            WslPipCacheInventory("Ubuntu", True, entrypoint, "/home/me/.cache/pip", "after"),
        ]
    )
    monkeypatch.setattr(
        wsl_pip,
        "inventory_wsl_pip_cache",
        lambda distribution, environment=None: next(inventories),
    )
    scope_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wsl_pip,
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
        if executable == "python3" and arguments == ("-m", "pip", "cache", "purge"):
            return _result(distribution, executable, arguments, "Files removed: 12 (42 MB)\n")
        raise AssertionError((executable, arguments))

    monkeypatch.setattr(wsl_pip, "run_wsl_exec", fake_exec)
    expected = WslPipCacheInventory(
        "Ubuntu",
        False,
        entrypoint,
        "/home/me/.cache/pip",
        "old display info",
    )

    result = purge_wsl_pip_cache(expected, {})

    assert scope_calls == [("Ubuntu", "/home/me/.cache/pip")]
    assert ("ps", ("-ww", "-eo", "comm=,args=")) in seen
    assert ("python3", ("-m", "pip", "cache", "purge")) in seen
    assert all(executable not in {"rm", "sh", "bash"} for executable, _arguments in seen)
    assert result.output.startswith("Files removed")
    assert result.after.cache_info == "after"


def test_purge_refuses_non_root_filesystem_before_vendor_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = WslPipEntrypoint("pip3", (), "pip 26.2 from /opt/pip")
    inventory = WslPipCacheInventory(
        "Ubuntu",
        True,
        entrypoint,
        "/mnt/share/pip-cache",
        "info",
    )
    monkeypatch.setattr(
        wsl_pip,
        "inventory_wsl_pip_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_pip,
        "_require_pip_idle",
        lambda distribution, environment=None: None,
    )
    monkeypatch.setattr(
        wsl_pip,
        "require_wsl_root_filesystem_path",
        lambda distribution, path, environment=None: (_ for _ in ()).throw(
            RuntimeError("mounted filesystem")
        ),
    )
    called = False

    def unexpected_pip(*args: object, **kwargs: object) -> WslExecResult:
        nonlocal called
        called = True
        raise AssertionError("vendor mutation should not run")

    monkeypatch.setattr(wsl_pip, "_run_pip", unexpected_pip)

    with pytest.raises(RuntimeError, match="mounted filesystem"):
        purge_wsl_pip_cache(inventory, {})
    assert not called


@pytest.mark.parametrize(
    "process_line",
    [
        "pip3 pip3 install requests",
        "python3 python3 -m pip install requests",
        "python3 /usr/bin/python3 /usr/local/bin/pip install requests",
    ],
)
def test_purge_refuses_when_pip_process_is_running(
    monkeypatch: pytest.MonkeyPatch,
    process_line: str,
) -> None:
    entrypoint = WslPipEntrypoint("pip3", (), "pip 26.2 from /opt/pip")
    inventory = WslPipCacheInventory(
        "Ubuntu",
        True,
        entrypoint,
        "/home/me/.cache/pip",
        "info",
    )
    monkeypatch.setattr(
        wsl_pip,
        "inventory_wsl_pip_cache",
        lambda distribution, environment=None: inventory,
    )
    monkeypatch.setattr(
        wsl_pip,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            process_line + "\n",
        ),
    )

    with pytest.raises(RuntimeError, match="appears to be running"):
        purge_wsl_pip_cache(inventory, {})


def test_purge_refuses_cache_identity_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = WslPipEntrypoint("pip3", (), "pip 26.2 from /opt/pip")
    expected = WslPipCacheInventory(
        "Ubuntu",
        True,
        entrypoint,
        "/home/me/.cache/pip",
        "info",
    )
    changed = WslPipCacheInventory(
        "Ubuntu",
        True,
        entrypoint,
        "/mnt/c/temp/pip-cache",
        "info",
    )
    monkeypatch.setattr(
        wsl_pip,
        "inventory_wsl_pip_cache",
        lambda distribution, environment=None: changed,
    )

    with pytest.raises(RuntimeError, match="changed before purge"):
        purge_wsl_pip_cache(expected, {})
