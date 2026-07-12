from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import reclaimer.adapters.vscode as vscode_module
from reclaimer.adapters.base import AdapterContext, ProbeStatus
from reclaimer.adapters.vscode import (
    VSCodeExtensionAdapter,
    discover_extension_roots,
    parse_extension_manifest,
)
from reclaimer.core.models import ProvenanceClass, RiskTier, SemanticType
from reclaimer.evidence.store import EvidenceStore

FIXTURES = Path(__file__).parent / "transcripts" / "vscode"


def _fixture(name: str = "valid-package.json") -> bytes:
    return (FIXTURES / name).read_bytes()


def _extension(root: Path, directory: str, *, payload: bytes | None = None) -> Path:
    extension = root / directory
    extension.mkdir(parents=True)
    (extension / "package.json").write_bytes(_fixture() if payload is None else payload)
    return extension


def _context(tmp_path: Path) -> AdapterContext:
    def forbidden_runner(command):
        raise AssertionError(f"VS Code vendor CLI must never run: {command.argv}")

    return AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        forbidden_runner,
    )


def test_vscode_manifest_parser_accepts_only_bounded_safe_fields() -> None:
    manifest = parse_extension_manifest(_fixture())

    assert manifest.publisher == "fixture-publisher"
    assert manifest.name == "fixture-extension"
    assert manifest.version == "1.2.3"
    assert manifest.vscode_engine == "^1.90.0"
    assert len(manifest.digest) == 64


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"publisher": "unsafe/publisher"}, "safe identifier"),
        ({"name": "fixture\u202ename"}, "safe text"),
        ({"engines": {}}, "engines.vscode"),
        ({"version": ""}, "version"),
    ],
)
def test_vscode_manifest_parser_rejects_untrusted_display_fields(
    mutation: dict[str, object], message: str
) -> None:
    payload = json.loads(_fixture())
    payload.update(mutation)

    with pytest.raises(ValueError, match=message):
        parse_extension_manifest(json.dumps(payload).encode())


def test_vscode_manifest_parser_rejects_oversized_input() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        parse_extension_manifest(b" " * (1024 * 1024 + 1))


def test_vscode_discovers_only_existing_safe_user_extension_roots(
    tmp_path: Path,
) -> None:
    stable = tmp_path / ".vscode" / "extensions"
    insiders = tmp_path / ".vscode-insiders" / "extensions"
    custom = tmp_path / "custom-extensions"
    for root in (stable, insiders, custom):
        root.mkdir(parents=True)

    roots, issues = discover_extension_roots(
        {
            "USERPROFILE": str(tmp_path),
            "VSCODE_EXTENSIONS": str(custom),
        }
    )

    assert {item.path for item in roots} == {stable, insiders, custom}
    assert {item.channel for item in roots} == {"stable", "insiders", "custom"}
    assert issues == ()


def test_vscode_adapter_is_filesystem_only_red_and_non_actionable(tmp_path: Path) -> None:
    stable = tmp_path / ".vscode" / "extensions"
    insiders = tmp_path / ".vscode-insiders" / "extensions"
    custom = tmp_path / "custom-extensions"
    stable_extension = _extension(stable, "fixture-publisher.fixture-extension-1.2.3")
    (stable_extension / "payload.bin").write_bytes(b"extension payload")
    _extension(insiders, "fixture-publisher.insiders-extension-1.2.3")
    _extension(custom, "fixture-publisher.custom-extension-1.2.3")
    (stable / ".obsolete").write_text(
        '{"fixture-publisher.fixture-extension-1.2.3": true}',
        encoding="utf-8",
    )

    # These are valuable user state, outside every approved extension root.
    global_storage = tmp_path / ".vscode" / "User" / "globalStorage" / "vendor.state"
    history = tmp_path / ".vscode" / "User" / "History" / "entry"
    _extension(global_storage, "must-not-be-scanned")
    _extension(history, "must-not-be-scanned")

    result = VSCodeExtensionAdapter(
        environment={
            "USERPROFILE": str(tmp_path),
            "VSCODE_EXTENSIONS": str(custom),
        }
    ).inventory(_context(tmp_path))

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 4
    assert result.evidence == ()
    assert all(resource.risk_tier is RiskTier.RED for resource in result.resources)
    assert all(
        resource.provenance_class is ProvenanceClass.UNKNOWN
        and resource.actionable is False
        for resource in result.resources
    )
    assert not any(
        "globalStorage" in (resource.path or "") or "History" in (resource.path or "")
        for resource in result.resources
    )

    extensions = [
        resource
        for resource in result.resources
        if resource.display_name.startswith("VS Code extension fixture-")
    ]
    assert len(extensions) == 3
    assert all(resource.semantic_type is SemanticType.APP_STATE for resource in extensions)
    assert all(resource.reconstruction.value == "NONE" for resource in extensions)
    assert all(resource.logical_size.value is not None for resource in extensions)
    marker = next(
        resource
        for resource in result.resources
        if resource.display_name.endswith(".obsolete marker")
    )
    assert marker.semantic_type is SemanticType.APP_STATE
    assert any("not interpreted" in warning for warning in marker.warnings)


def test_vscode_invalid_and_missing_manifests_stay_unknown(tmp_path: Path) -> None:
    root = tmp_path / ".vscode" / "extensions"
    invalid = _extension(
        root,
        "invalid-extension",
        payload=b'{"name":"bad","publisher":"bad","version":"1"}',
    )
    missing = root / "missing-manifest"
    missing.mkdir()
    (invalid / "unrelated.bin").write_bytes(b"\xff\xfe\x00not-json")

    result = VSCodeExtensionAdapter(
        environment={"USERPROFILE": str(tmp_path)}
    ).inventory(_context(tmp_path))

    assert len(result.resources) == 2
    assert all(
        resource.semantic_type is SemanticType.UNKNOWN
        and resource.risk_tier is RiskTier.RED
        and resource.vendor_locator is None
        and resource.actionable is False
        for resource in result.resources
    )
    assert [issue.code for issue in result.issues].count("MANIFEST_REJECTED") == 2


def test_vscode_oversized_manifest_is_not_parsed(tmp_path: Path) -> None:
    root = tmp_path / ".vscode" / "extensions"
    _extension(root, "oversized", payload=b" " * (1024 * 1024 + 1))

    result = VSCodeExtensionAdapter(
        environment={"USERPROFILE": str(tmp_path)}
    ).inventory(_context(tmp_path))

    assert result.resources[0].semantic_type is SemanticType.UNKNOWN
    assert any(issue.code == "MANIFEST_REJECTED" for issue in result.issues)


def test_vscode_relative_custom_root_is_rejected_without_cli(tmp_path: Path) -> None:
    result = VSCodeExtensionAdapter(
        environment={"VSCODE_EXTENSIONS": "relative/extensions"}
    ).inventory(_context(tmp_path))

    assert result.resources == ()
    assert result.probe.status is ProbeStatus.UNAVAILABLE
    assert {issue.code for issue in result.issues} == {
        "CUSTOM_ROOT_UNSAFE",
        "NO_EXTENSION_ROOT",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary test")
def test_vscode_junction_extension_is_reported_but_never_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ".vscode" / "extensions"
    target = tmp_path / "external-extension"
    junction = root / "fixture-publisher.redirected-1.0.0"
    root.mkdir(parents=True)
    _extension(tmp_path, "external-extension")
    (target / "protected-canary.bin").write_bytes(b"must not be traversed")

    created = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation unavailable: {created.stderr or created.stdout}")

    def forbidden_measure(path: Path):
        raise AssertionError(f"reparse extension must not be measured: {path}")

    monkeypatch.setattr(vscode_module, "measure_tree", forbidden_measure)
    result = VSCodeExtensionAdapter(
        environment={"USERPROFILE": str(tmp_path)}
    ).inventory(_context(tmp_path))

    assert len(result.resources) == 1
    resource = result.resources[0]
    assert resource.path == str(junction)
    assert resource.semantic_type is SemanticType.UNKNOWN
    assert resource.logical_size.value is None
    assert resource.vendor_locator is None
    assert any(issue.code == "EXTENSION_BOUNDARY_REJECTED" for issue in result.issues)
