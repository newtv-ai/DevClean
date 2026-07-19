from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from devclean.gates.broker_install import (
    ArtifactKind,
    parse_authenticode_json,
    parse_manifest_bytes,
    probe_mutation_rights,
)


def _manifest(*, artifacts: list[dict[str, str]] | None = None) -> bytes:
    payload = {
        "schema_version": "1.0.0",
        "release_id": "0.5.0-rc.1",
        "publisher_thumbprint": "a" * 40,
        "installer_sha256": "b" * 64,
        "artifacts": artifacts
        or [
            {
                "relative_path": "DevClean.Broker.exe",
                "kind": "BROKER",
                "sha256": "c" * 64,
            },
            {
                "relative_path": "lib/DevClean.Fixed.dll",
                "kind": "DLL",
                "sha256": "d" * 64,
            },
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def test_broker_manifest_parser_accepts_closed_canonical_contract() -> None:
    manifest = parse_manifest_bytes(_manifest())

    assert manifest.release_id == "0.5.0-rc.1"
    assert manifest.publisher_thumbprint == "a" * 40
    assert manifest.artifacts[0].kind is ArtifactKind.BROKER
    assert manifest.artifacts[1].relative_path == "lib/DevClean.Fixed.dll"


@pytest.mark.parametrize(
    "relative_path",
    (
        "../broker.exe",
        "/broker.exe",
        "bin\\broker.exe",
        "C:/broker.exe",
        "bin//broker.exe",
        "bin/./broker.exe",
        "NUL.dll",
        "broker.exe.",
        " broker.exe",
    ),
)
def test_broker_manifest_rejects_noncanonical_windows_paths(relative_path: str) -> None:
    with pytest.raises(ValueError):
        parse_manifest_bytes(
            _manifest(
                artifacts=[
                    {
                        "relative_path": relative_path,
                        "kind": "BROKER",
                        "sha256": "c" * 64,
                    }
                ]
            )
        )


def test_broker_manifest_rejects_duplicate_casefold_path_and_external_data() -> None:
    duplicate = [
        {
            "relative_path": "DevClean.Broker.exe",
            "kind": "BROKER",
            "sha256": "c" * 64,
        },
        {
            "relative_path": "DevClean.broker.EXE",
            "kind": "BROKER",
            "sha256": "d" * 64,
        },
    ]
    with pytest.raises(ValueError, match="collide"):
        parse_manifest_bytes(_manifest(artifacts=duplicate))

    payload = json.loads(_manifest())
    payload["artifacts"].append(
        {"relative_path": "actions.json", "kind": "DATA", "sha256": "e" * 64}
    )
    with pytest.raises(ValueError, match="kind"):
        parse_manifest_bytes(json.dumps(payload).encode())


def test_broker_manifest_rejects_duplicate_json_keys_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_manifest_bytes(
            b'{"schema_version":"1.0.0","schema_version":"1.0.0"}'
        )

    payload = json.loads(_manifest())
    payload["extra"] = True
    with pytest.raises(ValueError, match="keys"):
        parse_manifest_bytes(json.dumps(payload).encode())


def test_authenticode_observation_parser_is_strict() -> None:
    observation = parse_authenticode_json(
        b'{"signature_type":"Authenticode","status":"Valid",'
        b'"thumbprint":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}'
    )

    assert observation.status == "Valid"
    assert observation.signature_type == "Authenticode"
    assert observation.thumbprint == "a" * 40

    with pytest.raises(ValueError, match="unexpected"):
        parse_authenticode_json(
            b'{"signature_type":"Authenticode","status":"Valid",'
            b'"thumbprint":"aa","subject":"sensitive"}'
        )

    with pytest.raises(ValueError, match="thumbprint"):
        parse_authenticode_json(
            b'{"signature_type":"Authenticode","status":"Valid",'
            b'"thumbprint":"aa"}'
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows access-right integration test")
def test_mutation_probe_detects_current_users_writable_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "writable.bin"
    fixture.write_bytes(hashlib.sha256(b"fixture").digest())

    directory_rights = set(probe_mutation_rights(tmp_path))
    file_rights = set(probe_mutation_rights(fixture))

    assert "WRITE_DATA_OR_ADD_FILE" in directory_rights
    assert "WRITE_DATA_OR_ADD_FILE" in file_rights
    assert "DELETE" in file_rights
