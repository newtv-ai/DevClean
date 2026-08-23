from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import devclean.core.pnpm_maintenance as pnpm_maintenance
from devclean.core.pnpm_maintenance import inventory_pnpm_storage, prune_pnpm_store


def _environment(tmp_path: Path, store: Path) -> dict[str, str]:
    return {
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "roaming"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "TEMP": str(tmp_path / "temp"),
        "PNPM_HOME": str(tmp_path / "pnpm-home"),
        "PNPM_CONFIG_STORE_DIR": str(store),
        "PNPM_CONFIG_CACHE_DIR": str(tmp_path / "real-cache"),
        "PNPM_CONFIG_GLOBAL_DIR": str(tmp_path / "real-global"),
        "PNPM_CONFIG_DLX_CACHE_MAX_AGE": "1",
        "DEVCLEAN_PNPM_EXE": "pnpm-test",
    }


def _config_or_none(
    command: list[str],
    kwargs: dict[str, Any],
) -> subprocess.CompletedProcess[str] | None:
    if command[:3] != ["pnpm-test", "config", "get"]:
        return None
    env = kwargs["env"]
    key = command[3]
    values = {
        "cache-dir": env["PNPM_CONFIG_CACHE_DIR"],
        "global-dir": env["PNPM_CONFIG_GLOBAL_DIR"],
        "dlx-cache-max-age": env["PNPM_CONFIG_DLX_CACHE_MAX_AGE"],
    }
    return subprocess.CompletedProcess(command, 0, stdout=f"{values[key]}\n", stderr="")


def test_inventory_collapses_versioned_store_path_to_store_root(
    tmp_path: Path,
) -> None:
    store = tmp_path / "pnpm-home" / "store"
    version = store / "v10"
    version.mkdir(parents=True)
    (version / "blob").write_bytes(b"x" * 300)
    env = _environment(tmp_path, store)

    inventory = inventory_pnpm_storage(env)
    visible = [entry for entry in inventory.stores if entry.exists]

    assert len(visible) == 1
    assert visible[0].path == store
    assert visible[0].logical_bytes == 300
    assert not visible[0].recommended


def test_inventory_recommends_large_store_as_worthwhile_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    env = _environment(tmp_path, store)
    monkeypatch.setattr(
        pnpm_maintenance,
        "_directory_bytes",
        lambda _path: 2 * 1024**3,
    )

    inventory = inventory_pnpm_storage(env)
    visible = [entry for entry in inventory.stores if entry.exists]

    assert len(visible) == 1
    assert visible[0].recommended
    assert inventory.recommended_bytes == 2 * 1024**3


def test_prune_sandboxes_secondary_vendor_mutation_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    version = store / "v11"
    version.mkdir(parents=True)
    blob = version / "orphan"
    blob.write_bytes(b"x" * 500)
    env = _environment(tmp_path, store)
    calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        call_env = dict(kwargs["env"])
        calls.append((command, call_env))
        config = _config_or_none(command, kwargs)
        if config is not None:
            return config
        if command[-3:] == ["store", "path", "--silent"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{version}\n",
                stderr="",
            )
        assert command[-2:] == ["store", "prune"]
        blob.unlink()
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="Removed 500 B\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = prune_pnpm_store(store, env)

    expected_prefix = ["pnpm-test", "--store-dir", str(store)]
    commands = [command for command, _call_env in calls]
    assert commands == [
        ["pnpm-test", "config", "get", "cache-dir"],
        ["pnpm-test", "config", "get", "global-dir"],
        ["pnpm-test", "config", "get", "dlx-cache-max-age"],
        [*expected_prefix, "store", "path", "--silent"],
        [*expected_prefix, "store", "path", "--silent"],
        [*expected_prefix, "store", "prune"],
    ]

    prune_env = calls[-1][1]
    assert prune_env["PNPM_CONFIG_DLX_CACHE_MAX_AGE"] == "Infinity"
    assert prune_env["PNPM_CONFIG_CACHE_DIR"] != env["PNPM_CONFIG_CACHE_DIR"]
    assert prune_env["PNPM_CONFIG_GLOBAL_DIR"] != env["PNPM_CONFIG_GLOBAL_DIR"]
    assert "devclean-pnpm-prune-" in prune_env["PNPM_CONFIG_CACHE_DIR"]
    assert "devclean-pnpm-prune-" in prune_env["PNPM_CONFIG_GLOBAL_DIR"]

    assert result.store_path == store
    assert result.before_bytes == 500
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 500
    assert result.command == (*expected_prefix, "store", "prune")
    assert "Removed 500 B" in result.output


def test_prune_refuses_unrecognized_store_before_running_pnpm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audited = tmp_path / "audited-store"
    arbitrary = tmp_path / "other-store"
    audited.mkdir()
    arbitrary.mkdir()
    env = _environment(tmp_path, audited)

    def should_not_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("pnpm should not run for an unrecognized store")

    monkeypatch.setattr(subprocess, "run", should_not_run)

    with pytest.raises(ValueError, match="已审计"):
        prune_pnpm_store(arbitrary, env)


def test_prune_fails_closed_when_pnpm_rejects_sandbox_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    env = _environment(tmp_path, store)
    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: False)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == ["pnpm-test", "config", "get", "cache-dir"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{env['PNPM_CONFIG_CACHE_DIR']}\n",
                stderr="",
            )
        config = _config_or_none(command, kwargs)
        assert config is not None
        return config

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="隔离的 cache-dir"):
        prune_pnpm_store(store, env)
    assert calls == [["pnpm-test", "config", "get", "cache-dir"]]


def test_prune_fails_closed_when_pnpm_reports_a_different_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    env = _environment(tmp_path, store)
    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: False)
    store_path_calls = 0

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal store_path_calls
        config = _config_or_none(command, kwargs)
        if config is not None:
            return config
        assert command[-3:] == ["store", "path", "--silent"]
        store_path_calls += 1
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=str(tmp_path / "different-store" / "v11"),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="未确认所选 store"):
        prune_pnpm_store(store, env)
    assert store_path_calls == 1


def test_prune_revalidates_store_identity_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    version = store / "v11"
    version.mkdir(parents=True)
    env = _environment(tmp_path, store)
    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: False)
    identities = iter(((1, "first", "test"), (1, "changed", "test")))
    monkeypatch.setattr(pnpm_maintenance, "_store_identity", lambda _path: next(identities))
    prune_called = False

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal prune_called
        config = _config_or_none(command, kwargs)
        if config is not None:
            return config
        if command[-3:] == ["store", "path", "--silent"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(version), stderr="")
        prune_called = True
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="身份在执行前发生变化"):
        prune_pnpm_store(store, env)
    assert not prune_called


def test_prune_refuses_store_mutation_while_pnpm_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    env = _environment(tmp_path, store)
    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: True)

    with pytest.raises(RuntimeError, match="pnpm 正在运行"):
        prune_pnpm_store(store, env)


def test_prune_surfaces_vendor_failure_without_raw_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "store"
    version = store / "v11"
    version.mkdir(parents=True)
    payload = version / "keep.bin"
    payload.write_bytes(b"x" * 23)
    env = _environment(tmp_path, store)
    monkeypatch.setattr(pnpm_maintenance, "pnpm_process_running", lambda: False)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        config = _config_or_none(command, kwargs)
        if config is not None:
            return config
        if command[-3:] == ["store", "path", "--silent"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(version), stderr="")
        return subprocess.CompletedProcess(
            command,
            2,
            stdout="",
            stderr="store locked",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="store locked"):
        prune_pnpm_store(store, env)
    assert payload.exists()
