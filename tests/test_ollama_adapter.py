from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from devclean.adapters.base import AdapterContext, ProbeStatus
from devclean.adapters.ollama import (
    LoopbackRequestError,
    LoopbackResponse,
    OllamaAdapter,
    get_loopback,
    parse_ps_response,
    parse_tags_response,
    parse_version_response,
)
from devclean.core.models import Confidence, RiskTier
from devclean.evidence.store import EvidenceStore

FIXTURES = Path(__file__).parent / "transcripts" / "ollama"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_ollama_documented_shapes_parse_strictly() -> None:
    assert parse_version_response(_fixture("version.json")) == "0.12.6"

    models = parse_tags_response(_fixture("tags.json"))
    assert len(models) == 2
    assert models[0].size == 3_338_801_804
    assert models[0].digest == (
        "a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a"
    )

    running = parse_ps_response(_fixture("ps.json"))
    assert len(running) == 1
    assert running[0].size_vram == 5_333_539_264
    assert running[0].context_length == 4096


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"size": True}, "bounded non-negative"),
        ({"size": -1}, "bounded non-negative"),
        ({"digest": "short"}, "64 hexadecimal"),
        ({"name": "fixture\u202emodel"}, "safe text"),
        ({"modified_at": "yesterday"}, "ISO 8601"),
        ({"details": {"format": "gguf"}}, "family"),
    ],
)
def test_ollama_tags_rejects_malformed_records(
    mutation: dict[str, object], message: str
) -> None:
    payload = json.loads(_fixture("tags.json"))
    payload["models"][0].update(mutation)

    with pytest.raises(ValueError, match=message):
        parse_tags_response(json.dumps(payload).encode())


def test_ollama_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_tags_response(b'{"models": [], "models": []}')

    with pytest.raises(ValueError, match="not permitted"):
        parse_tags_response(b'{"models": [], "extension": NaN}')


def test_ollama_ps_rejects_invalid_runtime_values_and_duplicate_digest() -> None:
    payload = json.loads(_fixture("ps.json"))
    payload["models"][0]["size_vram"] = True
    with pytest.raises(ValueError, match="size_vram"):
        parse_ps_response(json.dumps(payload).encode())

    payload = json.loads(_fixture("ps.json"))
    payload["models"].append(dict(payload["models"][0]))
    with pytest.raises(ValueError, match="duplicate digest"):
        parse_ps_response(json.dumps(payload).encode())


def test_ollama_adapter_uses_only_three_loopback_gets_and_records_evidence(
    tmp_path: Path,
) -> None:
    bodies = {
        "/api/version": _fixture("version.json"),
        "/api/tags": _fixture("tags.json"),
        "/api/ps": _fixture("ps.json"),
    }
    calls: list[str] = []

    def http_get(endpoint: str) -> LoopbackResponse:
        calls.append(endpoint)
        return LoopbackResponse(200, "application/json; charset=utf-8", None, bodies[endpoint], 1)

    def forbidden_runner(command: Any) -> Any:
        raise AssertionError(f"Ollama must not invoke a CLI: {command!r}")

    evidence_root = tmp_path / "evidence"
    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=evidence_root),
        forbidden_runner,
    )
    result = OllamaAdapter(http_get).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert result.probe.version == "0.12.6"
    assert calls == ["/api/version", "/api/tags", "/api/ps"]
    assert len(result.resources) == 2
    assert all(resource.actionable is False for resource in result.resources)
    assert all(
        resource.logical_size.confidence is Confidence.ESTIMATE
        for resource in result.resources
    )
    assert all(resource.allocated_size.value is None for resource in result.resources)
    assert all(resource.exclusive_host_reclaimable.value is None for resource in result.resources)
    assert all(resource.vendor_logical_reclaimable.value is None for resource in result.resources)
    assert result.resources[0].risk_tier is RiskTier.RED
    assert result.resources[1].risk_tier is RiskTier.YELLOW
    assert any("currently loaded" in warning for warning in result.resources[0].warnings)
    assert all(len(resource.evidence) == 3 for resource in result.resources)
    assert len(result.evidence) == 3
    assert all(item.kind.value == "LOOPBACK_API" for item in result.evidence)

    loopback = evidence_root / "scan_fixture" / "loopback"
    metadata_files = sorted(loopback.glob("*.meta.json"))
    response_files = sorted(loopback.glob("*.response.redacted.txt"))
    assert len(metadata_files) == 3
    assert len(response_files) == 3
    metadata = [json.loads(path.read_text(encoding="utf-8")) for path in metadata_files]
    assert {item["endpoint"] for item in metadata} == set(bodies)
    assert all(item["kind"] == "LOOPBACK_API" for item in metadata)
    assert all(item["host"] == "127.0.0.1" and item["port"] == 11434 for item in metadata)
    assert all(item["method"] == "GET" and item["outcome"] == "RESPONSE" for item in metadata)
    assert all(item["http_status"] == 200 for item in metadata)
    assert all("redirect" not in item for item in metadata)
    assert all(item["response_file"].endswith(".response.redacted.txt") for item in metadata)


def test_ollama_unavailable_never_starts_service_or_calls_runner(tmp_path: Path) -> None:
    calls: list[str] = []

    def unavailable(endpoint: str) -> LoopbackResponse:
        calls.append(endpoint)
        raise ConnectionRefusedError

    def forbidden_runner(command: Any) -> Any:
        raise AssertionError(f"Ollama must not invoke a CLI: {command!r}")

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        forbidden_runner,
    )
    result = OllamaAdapter(unavailable).inventory(context)

    assert result.probe.status is ProbeStatus.UNAVAILABLE
    assert result.resources == ()
    assert len(result.evidence) == 1
    assert calls == ["/api/version"]
    metadata_files = list(
        (tmp_path / "evidence" / "scan_fixture" / "loopback").glob("*.meta.json")
    )
    assert len(metadata_files) == 1
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["outcome"] == "FAILURE"
    assert metadata["error_type"] == "ConnectionRefusedError"
    response_file = tmp_path / "evidence" / "scan_fixture" / metadata["response_file"]
    assert response_file.exists()
    assert metadata["response_sha256"] == metadata["response_stored_sha256"]


def test_ollama_http_is_fixed_to_ipv4_loopback_and_does_not_follow_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request: dict[str, Any] = {}

    class FakeResponse:
        status = 302

        def getheader(self, name: str) -> str | None:
            return {
                "Content-Length": "2",
                "Content-Type": "application/json",
                "Content-Encoding": None,
            }.get(name)

        def read(self, amount: int) -> bytes:
            request["read_limit"] = amount
            return b"{}"

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            request.update(host=host, port=port, timeout=timeout)

        def request_method(
            self,
            method: str,
            endpoint: str,
            *,
            body: None,
            headers: dict[str, str],
        ) -> None:
            request.update(method=method, endpoint=endpoint, body=body, headers=headers)

        request = request_method

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            request["closed"] = True

    monkeypatch.setattr(
        "devclean.adapters.ollama.http.client.HTTPConnection", FakeConnection
    )

    response = get_loopback("/api/version")

    assert response.status == 302
    assert request["host"] == "127.0.0.1"
    assert request["port"] == 11434
    assert request["method"] == "GET"
    assert request["endpoint"] == "/api/version"
    assert request["body"] is None
    assert request["headers"]["Accept-Encoding"] == "identity"
    assert request["read_limit"] == 64 * 1024 + 1
    assert request["closed"] is True


def test_ollama_http_rejects_oversized_content_length_before_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    class FakeResponse:
        status = 200

        def getheader(self, name: str) -> str | None:
            if name == "Content-Length":
                return str(64 * 1024 + 1)
            return "application/json" if name == "Content-Type" else None

        def read(self, amount: int) -> bytes:
            raise AssertionError(f"oversized body must not be read: {amount}")

    class FakeConnection:
        def __init__(self, host: str, port: int, *, timeout: float) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(
        "devclean.adapters.ollama.http.client.HTTPConnection", FakeConnection
    )

    with pytest.raises(LoopbackRequestError, match="ResponseLimitExceeded") as captured:
        get_loopback("/api/version")
    assert captured.value.output_limit_exceeded is True
    assert closed is True


def test_ollama_adapter_rejects_redirect_response_without_following_it(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def redirect(endpoint: str) -> LoopbackResponse:
        calls.append(endpoint)
        return LoopbackResponse(302, "application/json", None, b"{}", 1)

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        lambda command: (_ for _ in ()).throw(AssertionError(command)),
    )
    result = OllamaAdapter(redirect).inventory(context)

    assert result.probe.status is ProbeStatus.ERROR
    assert calls == ["/api/version"]
    metadata_file = next(
        (tmp_path / "evidence" / "scan_fixture" / "loopback").glob("*.meta.json")
    )
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    assert metadata["http_status"] == 302
    assert metadata["outcome"] == "RESPONSE"
    assert len(result.evidence) == 1
