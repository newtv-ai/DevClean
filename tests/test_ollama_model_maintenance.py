from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import devclean.core.ollama_model_maintenance as ollama_models
from devclean.core.ollama_model_maintenance import (
    delete_ollama_model,
    inventory_ollama_models,
    ollama_api_endpoint,
)


def _tags(*, digest: str = "sha256:abc") -> dict[str, Any]:
    return {
        "models": [
            {
                "name": "qwen3:8b",
                "model": "qwen3:8b",
                "modified_at": "2026-08-18T12:00:00Z",
                "size": 5_000_000_000,
                "digest": digest,
                "details": {
                    "family": "qwen3",
                    "parameter_size": "8.2B",
                    "quantization_level": "Q4_K_M",
                },
            },
            {
                "name": "gemma3:4b",
                "model": "gemma3:4b",
                "modified_at": "2026-08-17T12:00:00Z",
                "size": 3_000_000_000,
                "digest": "sha256:def",
                "details": {"family": "gemma3"},
            },
        ]
    }


def test_endpoint_defaults_to_loopback() -> None:
    assert ollama_api_endpoint({}) == "http://127.0.0.1:11434"


def test_endpoint_maps_wildcard_bind_to_loopback() -> None:
    assert ollama_api_endpoint({"OLLAMA_HOST": "0.0.0.0:22434"}) == "http://127.0.0.1:22434"


def test_endpoint_refuses_remote_host() -> None:
    with pytest.raises(ValueError, match="非 loopback"):
        ollama_api_endpoint({"OLLAMA_HOST": "http://192.168.1.50:11434"})


def test_inventory_uses_vendor_api_and_marks_loaded_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        del kwargs
        calls.append((method, route))
        assert endpoint == "http://127.0.0.1:11434"
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/tags":
            return _tags()
        if route == "/api/ps":
            return {
                "models": [
                    {
                        "name": "gemma3:4b",
                        "model": "gemma3:4b",
                        "digest": "sha256:def",
                    }
                ]
            }
        raise AssertionError(route)

    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("C:/models"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: True)

    inventory = inventory_ollama_models({})

    assert inventory.version == "0.11.0"
    assert inventory.deletion_supported
    assert inventory.logical_model_bytes == 8_000_000_000
    assert [model.name for model in inventory.models] == ["qwen3:8b", "gemma3:4b"]
    assert not inventory.models[0].running
    assert inventory.models[1].running
    assert inventory.models[0].parameter_size == "8.2B"
    assert calls == [
        ("GET", "/api/version"),
        ("GET", "/api/tags"),
        ("GET", "/api/ps"),
    ]


def test_inventory_keeps_shared_model_store_non_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        del endpoint, method, kwargs
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/tags":
            return _tags()
        return {"models": []}

    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("Z:/shared"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: False)

    inventory = inventory_ollama_models({})

    assert not inventory.deletion_supported


def test_delete_revalidates_digest_and_uses_delete_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object]] = []
    tag_calls = 0

    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal tag_calls
        calls.append((method, route, kwargs.get("payload")))
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/ps":
            return {"models": []}
        if route == "/api/tags":
            tag_calls += 1
            return _tags() if tag_calls == 1 else {"models": [_tags()["models"][1]]}
        if route == "/api/delete":
            assert endpoint == "http://127.0.0.1:11434"
            assert method == "DELETE"
            assert kwargs.get("payload") == {"model": "qwen3:8b"}
            return {}
        raise AssertionError(route)

    sizes = iter((9_000_000_000, 6_000_000_000))
    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("C:/models"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(ollama_models, "_model_store_bytes", lambda root: next(sizes))

    result = delete_ollama_model(
        "qwen3:8b",
        expected_digest="sha256:abc",
        environment={},
    )

    assert result.model == "qwen3:8b"
    assert result.logical_model_bytes == 5_000_000_000
    assert result.measured_reclaimed_bytes == 3_000_000_000
    assert ("DELETE", "/api/delete", {"model": "qwen3:8b"}) in calls


def test_delete_refuses_digest_change_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = False

    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal deleted
        del endpoint, method, kwargs
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/tags":
            return _tags(digest="sha256:changed")
        if route == "/api/ps":
            return {"models": []}
        if route == "/api/delete":
            deleted = True
            return {}
        raise AssertionError(route)

    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("C:/models"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: True)

    with pytest.raises(ValueError, match="已被替换"):
        delete_ollama_model("qwen3:8b", expected_digest="sha256:abc", environment={})

    assert not deleted


def test_delete_refuses_loaded_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        del endpoint, method, kwargs
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/tags":
            return _tags()
        if route == "/api/ps":
            return {"models": [{"name": "qwen3:8b", "digest": "sha256:abc"}]}
        raise AssertionError(route)

    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("C:/models"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: True)

    with pytest.raises(RuntimeError, match="当前已加载"):
        delete_ollama_model("qwen3:8b", expected_digest="sha256:abc", environment={})


def test_delete_refuses_shared_or_remote_model_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_request(
        endpoint: str,
        method: str,
        route: str,
        **kwargs: object,
    ) -> dict[str, object]:
        del endpoint, method, kwargs
        if route == "/api/version":
            return {"version": "0.11.0"}
        if route == "/api/tags":
            return _tags()
        return {"models": []}

    monkeypatch.setattr(ollama_models, "_json_request", fake_request)
    monkeypatch.setattr(ollama_models, "_model_root", lambda environment: Path("Z:/models"))
    monkeypatch.setattr(ollama_models, "is_local_fixed_path", lambda path: False)

    with pytest.raises(ValueError, match="本地固定磁盘"):
        delete_ollama_model("qwen3:8b", expected_digest="sha256:abc", environment={})
