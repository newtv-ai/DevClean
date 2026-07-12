from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from reclaimer.adapters.base import AdapterContext, ProbeStatus
from reclaimer.adapters.huggingface import HuggingFaceAdapter, parse_cache_inventory
from reclaimer.core.models import Confidence, SemanticType
from reclaimer.evidence.store import EvidenceStore
from reclaimer.platform.windows.process import (
    BoundedProcessResult,
    ProcessTermination,
)

FIXTURES = Path(__file__).parent / "transcripts" / "huggingface"
CACHE_ROOT = Path(r"G:\fixtures\hf\hub")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_hf_shape_a_keeps_exact_vendor_logical_size() -> None:
    resources = parse_cache_inventory(
        _fixture("shape-a-1.7.2.json"), cache_root=CACHE_ROOT, version=(1, 7, 2)
    )

    assert len(resources) == 1
    resource = resources[0]
    assert resource.semantic_type is SemanticType.INSTALLED_MODEL
    assert resource.logical_size.value == 123456789
    assert resource.logical_size.confidence is Confidence.EXACT
    assert resource.allocated_size.value is None
    assert resource.vendor_locator == (
        "model:fixture-org/model-a@1111111111111111111111111111111111111111"
    )
    assert resource.actionable is False


def test_hf_shape_b_parses_decimal_human_size_as_estimate() -> None:
    resource = parse_cache_inventory(
        _fixture("shape-b-1.23.0.json"), cache_root=CACHE_ROOT, version=(1, 23, 0)
    )[0]

    assert resource.logical_size.value == 117_700_000
    assert resource.logical_size.confidence is Confidence.ESTIMATE
    assert any("rounded" in warning for warning in resource.warnings)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"revision": "short"}, "40-character"),
        ({"snapshot_path": r"G:\outside\payload"}, "escapes"),
        ({"size_on_disk": -1}, "non-negative"),
        ({"repo_id": "fixture\u202eorg/model"}, "safe text"),
    ],
)
def test_hf_shape_validation_fails_closed(
    mutation: dict[str, object], message: str
) -> None:
    payload = json.loads(_fixture("shape-a-1.7.2.json"))
    payload[0].update(mutation)

    with pytest.raises(ValueError, match=message):
        parse_cache_inventory(
            json.dumps(payload), cache_root=CACHE_ROOT, version=(1, 7, 2)
        )


def test_hf_duplicate_revision_is_rejected() -> None:
    payload = json.loads(_fixture("shape-a-1.7.2.json"))
    payload.append(dict(payload[0]))

    with pytest.raises(ValueError, match="duplicate"):
        parse_cache_inventory(
            json.dumps(payload), cache_root=CACHE_ROOT, version=(1, 7, 2)
        )


def test_hf_json_shape_is_bound_to_tool_version() -> None:
    with pytest.raises(ValueError, match="bound to this version"):
        parse_cache_inventory(
            _fixture("shape-a-1.7.2.json"),
            cache_root=CACHE_ROOT,
            version=(1, 11, 0),
        )
    with pytest.raises(ValueError, match="bound to this version"):
        parse_cache_inventory(
            _fixture("shape-b-1.23.0.json"),
            cache_root=CACHE_ROOT,
            version=(1, 10, 0),
        )


def test_hf_future_major_is_rejected_before_capability_queries(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, b"hf 2.0.0\n")

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    result = HuggingFaceAdapter(
        executable=Path(sys.executable),
        environment={"HF_HUB_CACHE": str(tmp_path / "hub")},
    ).inventory(context)

    assert result.probe.status is ProbeStatus.UNSUPPORTED_VERSION
    assert len(commands) == 1


def test_hf_adapter_treats_missing_cache_as_available_and_empty(tmp_path: Path) -> None:
    responses = iter((b"hf 1.7.2\n", b"--revisions --format --cache-dir\n"))
    commands: list[tuple[str, ...]] = []

    def runner(command) -> BoundedProcessResult:
        commands.append(command.argv)
        return _success(command.argv, next(responses))

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        runner,
    )
    adapter = HuggingFaceAdapter(
        executable=Path(sys.executable),
        environment={
            "HF_HUB_CACHE": str(tmp_path / "missing-hub"),
            "USERPROFILE": str(tmp_path),
        },
    )

    result = adapter.inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert result.resources == ()
    assert result.issues[0].code == "CACHE_ROOT_MISSING"
    assert len(result.evidence) == 2
    assert all("rm" not in argv and "prune" not in argv for argv in commands)


def test_hf_adapter_runs_only_inventory_allowlist(tmp_path: Path) -> None:
    cache_root = tmp_path / "hub"
    snapshot = cache_root / "models--fixture--model" / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    inventory = json.dumps(
        [
            {
                "repo_id": "fixture/model",
                "repo_type": "model",
                "revision": "a" * 40,
                "snapshot_path": str(snapshot),
                "size_on_disk": 7,
                "last_accessed": 1.0,
                "last_modified": 1.0,
                "refs": ["main"],
            }
        ]
    ).encode()
    responses: Iterator[bytes] = iter(
        (b"huggingface_hub version 1.7.2\n", b"--revisions --format --cache-dir", inventory)
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
    result = HuggingFaceAdapter(
        executable=Path(sys.executable),
        environment={"HF_HUB_CACHE": str(cache_root)},
    ).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 1
    assert commands[-1][1:] == (
        "cache",
        "ls",
        "--revisions",
        "--format",
        "json",
        "--cache-dir",
        str(cache_root),
    )
    assert all("rm" not in argv and "prune" not in argv for argv in commands)


def _success(argv: tuple[str, ...], stdout: bytes) -> BoundedProcessResult:
    return BoundedProcessResult(
        argv=argv,
        returncode=0,
        stdout=stdout,
        stderr=b"",
        duration_ms=1,
        termination=ProcessTermination.EXITED,
    )
