from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from devclean.adapters.base import AdapterContext, ProbeStatus
from devclean.adapters.npm import NpmCacheAdapter, parse_cache_keys
from devclean.core.models import SemanticType
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)


def test_npm_key_parser_counts_without_returning_keys() -> None:
    text = (
        "make-fetch-happen:request-cache:https://registry.npmjs.org/pkg\n"
        "make-fetch-happen:request-cache:https://registry.npmjs.org/@scope/pkg\n"
    )
    assert parse_cache_keys(text) == 2
    assert parse_cache_keys("\r\n") == 0


@pytest.mark.parametrize(
    "text",
    [
        "https://user:password@example.invalid/pkg\n",
        "https://example.invalid/pkg?token=secret\n",
        "bad\x1b[31m\n",
        "bad\u202ename\n",
        "\nnot-empty\n",
    ],
)
def test_npm_key_parser_rejects_credentials_and_controls(text: str) -> None:
    with pytest.raises(ValueError):
        parse_cache_keys(text)


def test_npm_adapter_uses_direct_node_and_splits_top_level_components(
    tmp_path: Path,
) -> None:
    node, npm_cli = _installation(tmp_path)
    cache_root = tmp_path / "npm-cache"
    cacache = cache_root / "_cacache"
    logs = cache_root / "_logs"
    cacache.mkdir(parents=True)
    logs.mkdir()
    (cacache / "content.bin").write_bytes(b"content")
    (logs / "debug.log").write_text("log", encoding="utf-8")
    responses = iter(
        (
            b"9.8.1\n",
            b"Usage: npm cache; commands: add clean ls verify\n",
            f"{cache_root}\r\n".encode(),
            b"make-fetch-happen:request-cache:https://registry.npmjs.org/pkg\n",
        )
    )
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = NpmCacheAdapter(node, npm_cli).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert {resource.semantic_type for resource in result.resources} == {
        SemanticType.REBUILDABLE_CACHE,
        SemanticType.APP_STATE,
    }
    assert all(resource.actionable is False for resource in result.resources)
    assert any(issue.code == "INDEX_KEYS_OBSERVED" for issue in result.issues)
    assert len(result.evidence) == 4
    assert all(command[0] == str(node) and command[1] == str(npm_cli) for command in commands)
    assert all("verify" not in command and "clean" not in command for command in commands)
    assert "--logs-max=0" in commands[-1]
    assert "--offline" in commands[-1]


def test_npm_index_failure_keeps_only_report_only_root_occupancy(tmp_path: Path) -> None:
    node, npm_cli = _installation(tmp_path)
    cache_root = tmp_path / "npm-cache"
    (cache_root / "_cacache").mkdir(parents=True)
    responses = iter(
        (
            _success((str(node),), b"10.9.0\n"),
            _success((str(node),), b"cache ls\n"),
            _success((str(node),), f"{cache_root}\n".encode()),
            BoundedProcessResult(
                argv=(str(node),),
                returncode=1,
                stdout=b"partial",
                stderr=b"failed",
                duration_ms=1,
                termination=ProcessTermination.OUTPUT_LIMIT,
            ),
        )
    )

    def runner(command) -> BoundedProcessResult:
        response = next(responses)
        return BoundedProcessResult(
            argv=command.argv,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            duration_ms=response.duration_ms,
            termination=response.termination,
        )

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = NpmCacheAdapter(node, npm_cli).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert result.resources
    assert all(resource.actionable is False for resource in result.resources)
    assert any(issue.code == "INDEX_QUERY_FAILED" for issue in result.issues)


def test_npm_rejects_cli_outside_node_installation(tmp_path: Path) -> None:
    node = tmp_path / "node" / "node.exe"
    node.parent.mkdir()
    shutil.copy2(sys.executable, node)
    npm_cli = tmp_path / "other" / "npm-cli.js"
    npm_cli.parent.mkdir()
    npm_cli.write_text("// fixture", encoding="utf-8")
    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        lambda command: _success(command.argv, b""),
    )

    result = NpmCacheAdapter(node, npm_cli).inventory(context)

    assert result.probe.status is ProbeStatus.UNAVAILABLE


def _installation(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "node-install"
    node = root / "node.exe"
    npm_cli = root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cli.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, node)
    npm_cli.write_text("// synthetic npm CLI", encoding="utf-8")
    return (node.resolve(), npm_cli.resolve())


def _success(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
