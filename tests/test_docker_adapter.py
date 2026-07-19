from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from devclean.adapters.base import AdapterContext, ProbeStatus
from devclean.adapters.command import QueryCommand
from devclean.adapters.docker import DockerAdapter, parse_system_df_jsonl
from devclean.core.models import Confidence, RiskTier
from devclean.evidence.store import EvidenceStore
from devclean.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)

FIXTURE = Path(__file__).parent / "transcripts" / "docker" / "system-df.jsonl"


def test_docker_system_df_parser_accepts_exact_four_record_jsonl() -> None:
    usages = parse_system_df_jsonl(FIXTURE.read_text(encoding="utf-8"))

    assert [usage.resource_type for usage in usages] == [
        "Images",
        "Containers",
        "Local Volumes",
        "Build Cache",
    ]
    assert usages[0].size == 4_321_000_000
    assert usages[0].reclaimable == 1_250_000_000
    assert usages[0].reclaimable_percent == 28
    assert usages[-1].reclaimable_percent is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda records: records[:-1],
        lambda records: [*records[:-1], records[0]],
        lambda records: [
            {**records[0], "Type": "Networks"},
            *records[1:],
        ],
        lambda records: [
            {**records[0], "TotalCount": 12},
            *records[1:],
        ],
        lambda records: [
            {**records[0], "Reclaimable": "1.25 GiB (28%)"},
            *records[1:],
        ],
        lambda records: [
            {**records[0], "Active": "13"},
            *records[1:],
        ],
    ],
)
def test_docker_system_df_parser_fails_closed_on_shape_drift(
    mutator: Callable[
        [list[dict[str, object]]],
        list[dict[str, object]],
    ],
) -> None:
    records = [json.loads(line) for line in FIXTURE.read_text("utf-8").splitlines()]
    text = "\n".join(json.dumps(record) for record in mutator(records))

    with pytest.raises(ValueError):
        parse_system_df_jsonl(text)


def test_docker_adapter_forces_local_pipe_and_empty_config_sandbox(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "docker.exe"
    shutil.copy2(sys.executable, executable)
    responses = iter(
        (
            b"27.5.1\t27.5.1\n",
            b"Usage: docker system df [OPTIONS]\n      --format string\n",
            FIXTURE.read_bytes(),
        )
    )
    commands: list[tuple[str, ...]] = []

    def runner(command: QueryCommand) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = DockerAdapter(executable).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 4
    assert len(result.evidence) == 3
    assert all(resource.actionable is False for resource in result.resources)
    assert all(
        resource.logical_size.confidence is Confidence.ESTIMATE
        for resource in result.resources
    )
    assert {resource.risk_tier for resource in result.resources} == {
        RiskTier.YELLOW,
        RiskTier.RED,
    }
    assert all(command[0] == str(executable) for command in commands)
    assert all("--host" in command for command in commands)
    assert all("npipe:////./pipe/docker_engine" in command for command in commands)
    assert all("--config" in command for command in commands)
    assert all("prune" not in command for command in commands)
    assert all("--force" not in command and "-a" not in command for command in commands)
    assert (tmp_path / "evidence" / "scan_fixture" / "docker-config").is_dir()


def test_docker_daemon_unavailable_is_not_started(tmp_path: Path) -> None:
    executable = tmp_path / "docker.exe"
    shutil.copy2(sys.executable, executable)
    commands: list[tuple[str, ...]] = []

    def runner(command: QueryCommand) -> BoundedProcessResult:
        commands.append(command.argv)
        return BoundedProcessResult(
            argv=command.argv,
            returncode=1,
            stdout=b"",
            stderr=b"cannot connect",
            duration_ms=1,
            termination=ProcessTermination.EXITED,
        )

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = DockerAdapter(executable).inventory(context)

    assert result.probe.status is ProbeStatus.UNAVAILABLE
    assert len(commands) == 1
    assert "version" in commands[0]
    assert all("start" not in argument for argument in commands[0])
    assert result.resources == ()


def _success(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
