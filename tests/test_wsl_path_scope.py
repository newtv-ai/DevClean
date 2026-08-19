from __future__ import annotations

from collections.abc import Mapping

import pytest

import devclean.core.wsl_path_scope as wsl_scope
from devclean.core.wsl_exec import WslExecResult
from devclean.core.wsl_path_scope import require_wsl_root_filesystem_path


def _result(
    distribution: str,
    executable: str,
    arguments: tuple[str, ...],
    stdout: str,
) -> WslExecResult:
    return WslExecResult(
        distribution=distribution,
        executable=executable,
        arguments=arguments,
        command=("wsl-test", "--distribution", distribution, "--exec", executable, *arguments),
        stdout=stdout,
        stderr="",
    )


def test_root_filesystem_scope_requires_same_device_and_dereferences_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        return _result(distribution, executable, arguments, "2049\n")

    monkeypatch.setattr(wsl_scope, "run_wsl_exec", fake_exec)

    proof = require_wsl_root_filesystem_path("Ubuntu", "/home/me/.cache/pip", {})

    assert proof.root_device == proof.target_device == 2049
    assert proof.path == "/home/me/.cache/pip"
    assert seen == [
        ("stat", ("-L", "-c", "%d", "--", "/")),
        ("stat", ("-L", "-c", "%d", "--", "/home/me/.cache/pip")),
    ]
    assert all(executable not in {"sh", "bash"} for executable, _arguments in seen)


def test_root_filesystem_scope_refuses_other_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(["2049\n", "77\n"])
    monkeypatch.setattr(
        wsl_scope,
        "run_wsl_exec",
        lambda distribution, executable, arguments=(), environment=None, timeout=120: _result(
            distribution,
            executable,
            tuple(arguments),
            next(values),
        ),
    )

    with pytest.raises(RuntimeError, match="mounted filesystem"):
        require_wsl_root_filesystem_path("Ubuntu", "/mnt/share/cache", {})


def test_root_filesystem_scope_fails_closed_when_stat_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wsl_scope,
        "run_wsl_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stat missing")),
    )

    with pytest.raises(RuntimeError, match="Cannot prove WSL filesystem scope"):
        require_wsl_root_filesystem_path("Ubuntu", "/home/me/cache", {})


@pytest.mark.parametrize("path", ["", "relative/cache", "/", "bad\x00path"])
def test_root_filesystem_scope_rejects_invalid_targets(path: str) -> None:
    with pytest.raises(ValueError):
        require_wsl_root_filesystem_path("Ubuntu", path, {})
