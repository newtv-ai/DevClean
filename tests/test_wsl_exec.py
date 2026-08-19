from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence

import pytest

import devclean.core.wsl_exec as wsl_exec
from devclean.core.wsl_exec import run_wsl_exec, run_wsl_exec_with_env
from devclean.core.wsl_inventory import WslDistribution, WslInventory


def _inventory(*names: str) -> WslInventory:
    return WslInventory(
        executable="wsl-test",
        version_text="WSL version: test",
        status_text="",
        distributions=tuple(WslDistribution(name=name, running=False) for name in names),
    )


def _completed(
    args: Sequence[str],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        list(args),
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_wsl_exec_pins_exact_distro_and_uses_no_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([_inventory("Ubuntu"), _inventory("Ubuntu")])
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: next(inventories))
    monkeypatch.setattr(wsl_exec, "wsl_executable", lambda environment=None: "wsl-test")
    seen: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        del check, capture_output, text, timeout, env
        seen.append(list(command))
        return _completed(command, stdout=b"/home/user/.cache/pip\n")

    monkeypatch.setattr("devclean.core.wsl_exec.subprocess.run", fake_run)

    result = run_wsl_exec("ubuntu", "pip3", ("cache", "dir"), {})

    assert seen == [
        [
            "wsl-test",
            "--distribution",
            "Ubuntu",
            "--exec",
            "pip3",
            "cache",
            "dir",
        ]
    ]
    assert result.distribution == "Ubuntu"
    assert result.stdout == "/home/user/.cache/pip"
    assert "sh" not in result.command
    assert "bash" not in result.command


def test_wsl_exec_with_env_pins_linux_env_without_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([_inventory("Ubuntu"), _inventory("Ubuntu")])
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: next(inventories))
    monkeypatch.setattr(wsl_exec, "wsl_executable", lambda environment=None: "wsl-test")
    seen: list[list[str]] = []

    def fake_run(
        command: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        seen.append(list(command))
        return _completed(command, stdout=b"ok\n")

    monkeypatch.setattr("devclean.core.wsl_exec.subprocess.run", fake_run)

    result = run_wsl_exec_with_env(
        "ubuntu",
        "go",
        ("clean", "-cache"),
        {
            "GOCACHE": "/home/me/.cache/go-build",
            "GOCACHEPROG": "",
            "GOFLAGS": "",
        },
        {},
    )

    assert seen == [
        [
            "wsl-test",
            "--distribution",
            "Ubuntu",
            "--exec",
            "env",
            "GOCACHE=/home/me/.cache/go-build",
            "GOCACHEPROG=",
            "GOFLAGS=",
            "go",
            "clean",
            "-cache",
        ]
    ]
    assert result.executable == "go"
    assert result.arguments == ("clean", "-cache")
    assert "sh" not in result.command
    assert "bash" not in result.command


def test_wsl_exec_refuses_missing_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Debian"))

    with pytest.raises(RuntimeError, match="found=0"):
        run_wsl_exec("Ubuntu", "pip", ("cache", "dir"), {})


@pytest.mark.parametrize(
    "executable",
    [
        "sh",
        "/bin/bash",
        "sudo",
        "rm",
        "/usr/bin/truncate",
        "apt-get",
        "docker",
        "podman",
        "node",
        "pwsh",
        "env",
    ],
)
def test_wsl_exec_blocks_shell_raw_delete_admin_and_unreviewed_lifecycle_tools(
    monkeypatch: pytest.MonkeyPatch,
    executable: str,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="禁止直接执行"):
        run_wsl_exec("Ubuntu", executable, (), {})


def test_wsl_exec_with_env_cannot_wrap_forbidden_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="禁止直接执行"):
        run_wsl_exec_with_env("Ubuntu", "sh", ("-c", "echo unsafe"), {"GOCACHE": "/tmp"}, {})


def test_wsl_exec_with_env_rejects_invalid_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="name"):
        run_wsl_exec_with_env("Ubuntu", "go", (), {"BAD-NAME": "1"}, {})
    with pytest.raises(ValueError, match="NUL"):
        run_wsl_exec_with_env("Ubuntu", "go", (), {"GOCACHE": "bad\x00path"}, {})


@pytest.mark.parametrize("name", ["PATH", "LD_PRELOAD", "GOENV"])
def test_wsl_exec_with_env_rejects_unreviewed_environment_names(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="未审计"):
        run_wsl_exec_with_env("Ubuntu", "go", (), {name: "value"}, {})


@pytest.mark.parametrize(
    ("name", "value"),
    [("GOFLAGS", "-modcache"), ("GOCACHEPROG", "cache-helper")],
)
def test_wsl_exec_with_env_requires_control_overrides_to_be_empty(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="只允许空值"):
        run_wsl_exec_with_env("Ubuntu", "go", (), {name: value}, {})


def test_wsl_exec_allows_python_module_pip_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([_inventory("Ubuntu"), _inventory("Ubuntu")])
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: next(inventories))
    monkeypatch.setattr(wsl_exec, "wsl_executable", lambda environment=None: "wsl-test")
    monkeypatch.setattr(
        "devclean.core.wsl_exec.subprocess.run",
        lambda command, **kwargs: _completed(command, stdout=b"ok\n"),
    )

    result = run_wsl_exec(
        "Ubuntu",
        "python3",
        ("-m", "pip", "cache", "dir"),
        {},
    )

    assert result.arguments[:2] == ("-m", "pip")


def test_wsl_exec_rejects_python_dynamic_code_and_other_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="python -m pip"):
        run_wsl_exec("Ubuntu", "python3", ("-c", "print('x')"), {})
    with pytest.raises(ValueError, match="python -m pip"):
        run_wsl_exec("Ubuntu", "python", ("-m", "http.server"), {})


def test_wsl_exec_surfaces_vendor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))
    monkeypatch.setattr(wsl_exec, "wsl_executable", lambda environment=None: "wsl-test")
    monkeypatch.setattr(
        "devclean.core.wsl_exec.subprocess.run",
        lambda command, **kwargs: _completed(
            command,
            stderr=b"pip unavailable\n",
            returncode=2,
        ),
    )

    with pytest.raises(RuntimeError, match="pip unavailable"):
        run_wsl_exec("Ubuntu", "pip", ("cache", "dir"), {})


def test_wsl_exec_revalidates_distribution_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([_inventory("Ubuntu"), _inventory("Debian")])
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: next(inventories))
    monkeypatch.setattr(wsl_exec, "wsl_executable", lambda environment=None: "wsl-test")
    monkeypatch.setattr(
        "devclean.core.wsl_exec.subprocess.run",
        lambda command, **kwargs: _completed(command, stdout=b"ok\n"),
    )

    with pytest.raises(RuntimeError, match="found=0"):
        run_wsl_exec("Ubuntu", "pip", ("cache", "dir"), {})


def test_wsl_exec_rejects_nul_and_nonpositive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wsl_exec, "inspect_wsl", lambda environment=None: _inventory("Ubuntu"))

    with pytest.raises(ValueError, match="timeout"):
        run_wsl_exec("Ubuntu", "pip", (), {}, timeout=0)
    with pytest.raises(ValueError, match="NUL"):
        run_wsl_exec("Ubuntu", "pip", ("bad\x00arg",), {})
