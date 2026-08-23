from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.npm_maintenance as npm
from devclean.core.npm_maintenance import NpmPathIdentity
from devclean.scanner.filesystem import ScanOptions, scan_roots
from devclean.ui.product_vendor_app import _generic_vendor_skip_paths

_STABLE_KEYS = (
    "cache",
    "prefix",
    "userconfig",
    "globalconfig",
    "cafile",
    "init-module",
    "init.module",
    "node-gyp",
    "logs-dir",
)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, dict[str, Path]]:
    tool = tmp_path / "npm.cmd"
    tool.write_text("test", encoding="utf-8")
    cache = tmp_path / "npm-cache"
    children = {
        name: cache / name for name in ("_cacache", "_npx", "_tuf", "_logs")
    }
    for path in children.values():
        path.mkdir(parents=True)
    environment = {
        "DEVCLEAN_NPM_EXE": str(tool),
        "USERPROFILE": str(tmp_path / "profile"),
        "LOCALAPPDATA": str(tmp_path / "local"),
        "APPDATA": str(tmp_path / "roaming"),
        "TEMP": str(tmp_path / "temp"),
    }
    return environment, cache, children


def _mock_live_npm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cache: Path,
    tmp_path: Path,
    overrides: dict[str, Path | None] | None = None,
    omit: frozenset[str] = frozenset(),
) -> list[tuple[str, ...]]:
    values: dict[str, Path | None] = {
        "cache": cache,
        "prefix": tmp_path / "outside" / "prefix",
        "userconfig": tmp_path / "outside" / ".npmrc",
        "globalconfig": tmp_path / "outside" / "global.npmrc",
        "cafile": None,
        "init-module": tmp_path / "outside" / "npm-init.js",
        "init.module": None,
        "node-gyp": tmp_path / "outside" / "node-gyp.js",
        "logs-dir": tmp_path / "outside" / "logs",
        "global-ignore-file": tmp_path / "outside" / "global.npmignore",
    }
    if overrides:
        values.update(overrides)
    calls: list[tuple[str, ...]] = []

    def fake_run(
        tool: NpmPathIdentity,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        del tool, environment
        calls.append(arguments)
        assert timeout == 30
        if arguments == ("config", "get", "cache"):
            return subprocess.CompletedProcess([], 0, f"{cache}\n", "")
        if arguments == ("--version",):
            return subprocess.CompletedProcess([], 0, "12.0.2\n", "")
        assert arguments[:2] == ("config", "get")
        lines: list[str] = []
        for key in arguments[2:]:
            if key in omit:
                continue
            value = values[key]
            rendered = "null" if value is None else str(value)
            lines.append(f"{key}={rendered}")
        return subprocess.CompletedProcess([], 0, "\n".join(lines), "")

    monkeypatch.setattr(npm, "_run_npm", fake_run)
    monkeypatch.setattr(npm, "is_local_fixed_path", lambda _path: True)
    return calls


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_npm_scan_pruning_is_constant_cost_and_skips_only_provider_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, cache, children = _layout(tmp_path)
    calls = _mock_live_npm(monkeypatch, cache=cache, tmp_path=tmp_path)

    skipped = npm.npm_generic_scan_skip_paths(environment)

    assert set(skipped) == {
        children["_cacache"].resolve(),
        children["_npx"].resolve(),
        children["_tuf"].resolve(),
    }
    assert children["_logs"].resolve() not in skipped
    assert calls[0] == ("config", "get", "cache")
    assert calls[1] == ("--version",)
    assert calls[2] == (
        "config",
        "get",
        *_STABLE_KEYS,
        "global-ignore-file",
    )
    assert len(calls) == 3
    assert not any(call and call[0] == "cache" for call in calls)


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_npm_scan_pruning_keeps_only_overlapped_child_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, cache, children = _layout(tmp_path)
    _mock_live_npm(
        monkeypatch,
        cache=cache,
        tmp_path=tmp_path,
        overrides={"globalconfig": children["_cacache"] / "global.npmrc"},
    )

    skipped = set(npm.npm_generic_scan_skip_paths(environment))

    assert children["_cacache"].resolve() not in skipped
    assert children["_npx"].resolve() in skipped
    assert children["_tuf"].resolve() in skipped


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_npm_scan_pruning_keeps_unproven_boundary_child_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, cache, children = _layout(tmp_path)
    _mock_live_npm(monkeypatch, cache=cache, tmp_path=tmp_path)
    original_identity = npm._path_identity

    def boundary_identity(
        path: Path,
        *,
        expect_directory: bool,
        label: str,
    ) -> NpmPathIdentity:
        if Path(path) == children["_npx"]:
            raise RuntimeError("simulated reparse/cloud boundary")
        return original_identity(path, expect_directory=expect_directory, label=label)

    monkeypatch.setattr(npm, "_path_identity", boundary_identity)

    skipped = set(npm.npm_generic_scan_skip_paths(environment))

    assert children["_cacache"].resolve() in skipped
    assert children["_npx"].resolve() not in skipped
    assert children["_tuf"].resolve() in skipped


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_npm_scan_pruning_fails_closed_on_incomplete_live_boundary_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, cache, _children = _layout(tmp_path)
    _mock_live_npm(
        monkeypatch,
        cache=cache,
        tmp_path=tmp_path,
        omit=frozenset({"globalconfig"}),
    )

    with pytest.raises(RuntimeError, match="globalconfig"):
        npm.npm_generic_scan_skip_paths(environment)


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_generic_scan_still_sees_npm_logs_and_unclassified_root_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, cache, children = _layout(tmp_path)
    _mock_live_npm(monkeypatch, cache=cache, tmp_path=tmp_path)
    hidden_payload = children["_cacache"] / "payload.bin"
    hidden_payload.write_bytes(b"cache")
    debug_log = children["_logs"] / "2026-08-24T00_00_00_000Z-debug-0.log"
    debug_log.write_text("diagnostic", encoding="utf-8")
    unknown = cache / "future-state.db"
    unknown.write_bytes(b"keep")

    skipped = npm.npm_generic_scan_skip_paths(environment)
    records = tuple(
        scan_roots(
            (cache,),
            ScanOptions(skip_paths=frozenset(str(path) for path in skipped)),
        )
    )
    observed = {Path(record.path) for record in records}

    assert hidden_payload not in observed
    assert debug_log in observed
    assert unknown in observed


@pytest.mark.skipif(os.name != "nt", reason="npm scan pruning targets Windows paths")
def test_product_skip_helper_accepts_only_reachable_source_proven_paths(
    tmp_path: Path,
) -> None:
    reachable = tmp_path / "npm-cache" / "_cacache"
    unreachable = Path(r"Z:\npm-cache\_npx")
    reachable.mkdir(parents=True)

    skipped = set(
        _generic_vendor_skip_paths(
            (),
            (),
            (Path(tmp_path.anchor),),
            (reachable, unreachable),
        )
    )

    assert str(reachable) in skipped
    assert str(unreachable) not in skipped
