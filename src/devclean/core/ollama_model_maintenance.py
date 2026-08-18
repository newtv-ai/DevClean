"""Vendor-owned Ollama model inventory and explicit per-model removal."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devclean.core.ollama_cleanup import ollama_roots
from devclean.platform.windows.volumes import is_local_fixed_path

_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class OllamaModelEntry:
    name: str
    digest: str
    logical_bytes: int
    modified_at: str
    parameter_size: str
    quantization_level: str
    family: str
    running: bool


@dataclass(frozen=True, slots=True)
class OllamaModelInventory:
    endpoint: str
    version: str
    model_root: Path | None
    deletion_supported: bool
    models: tuple[OllamaModelEntry, ...]

    @property
    def logical_model_bytes(self) -> int:
        return sum(model.logical_bytes for model in self.models)


@dataclass(frozen=True, slots=True)
class OllamaModelDeleteResult:
    model: str
    digest: str
    logical_model_bytes: int
    store_before_bytes: int | None
    store_after_bytes: int | None

    @property
    def measured_reclaimed_bytes(self) -> int | None:
        if self.store_before_bytes is None or self.store_after_bytes is None:
            return None
        return max(0, self.store_before_bytes - self.store_after_bytes)


def inventory_ollama_models(
    environment: Mapping[str, str] | None = None,
) -> OllamaModelInventory:
    """List exact models through Ollama's local API; never parse blob filenames."""

    endpoint = ollama_api_endpoint(environment)
    version_payload = _json_request(endpoint, "GET", "/api/version")
    version = _required_string(version_payload, "version", "Ollama /api/version")

    tags_payload = _json_request(endpoint, "GET", "/api/tags")
    ps_payload = _json_request(endpoint, "GET", "/api/ps")
    running = _running_model_identities(ps_payload)

    raw_models = tags_payload.get("models")
    if not isinstance(raw_models, list):
        raise RuntimeError("Ollama /api/tags 缺少 models 数组")

    models: list[OllamaModelEntry] = []
    for value in raw_models:
        if not isinstance(value, dict):
            raise RuntimeError("Ollama /api/tags 返回了无法识别的模型条目")
        name = _required_string(value, "name", "Ollama model")
        digest = _required_string(value, "digest", f"Ollama model {name}")
        size = value.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError(f"Ollama model {name} 的 size 无效")
        details = value.get("details")
        details_map = details if isinstance(details, dict) else {}
        identities = {name, digest}
        model_alias = value.get("model")
        if isinstance(model_alias, str) and model_alias:
            identities.add(model_alias)
        models.append(
            OllamaModelEntry(
                name=name,
                digest=digest,
                logical_bytes=size,
                modified_at=_optional_string(value.get("modified_at")),
                parameter_size=_optional_string(details_map.get("parameter_size")),
                quantization_level=_optional_string(
                    details_map.get("quantization_level")
                ),
                family=_optional_string(details_map.get("family")),
                running=bool(identities & running),
            )
        )

    models.sort(key=lambda item: item.logical_bytes, reverse=True)
    model_root = _model_root(environment)
    deletion_supported = (
        model_root is not None and is_local_fixed_path(model_root)
    )
    return OllamaModelInventory(
        endpoint=endpoint,
        version=version,
        model_root=model_root,
        deletion_supported=deletion_supported,
        models=tuple(models),
    )


def delete_ollama_model(
    model: str,
    *,
    expected_digest: str,
    environment: Mapping[str, str] | None = None,
) -> OllamaModelDeleteResult:
    """Delete one exact model via Ollama after digest/running-state revalidation."""

    if not model or not expected_digest:
        raise ValueError("Ollama model 和 expected_digest 都必须提供")
    inventory = inventory_ollama_models(environment)
    if not inventory.deletion_supported:
        raise ValueError(
            "Ollama 模型目录不在本地固定磁盘上；共享、远程、可移动或 reparse "
            "重定向的模型库只允许检查"
        )
    selected = next((entry for entry in inventory.models if entry.name == model), None)
    if selected is None:
        raise FileNotFoundError(f"Ollama 模型已不存在: {model}")
    if selected.digest != expected_digest:
        raise ValueError(
            f"Ollama 模型 {model} 自检查后已被替换；请重新统计后再删除"
        )
    if selected.running:
        raise RuntimeError(
            f"Ollama 模型 {model} 当前已加载；请先让模型退出内存后再删除"
        )

    store_before = _model_store_bytes(inventory.model_root)
    _json_request(
        inventory.endpoint,
        "DELETE",
        "/api/delete",
        payload={"model": selected.name},
        timeout=120,
    )

    # Postcondition is checked through the same vendor API. Internal blob sharing
    # is intentionally left to Ollama; DevClean never tries to infer blob refs.
    after_payload = _json_request(inventory.endpoint, "GET", "/api/tags")
    after_models = after_payload.get("models")
    if not isinstance(after_models, list):
        raise RuntimeError("Ollama 删除后的 /api/tags 返回无效")
    for value in after_models:
        if isinstance(value, dict) and value.get("name") == selected.name:
            raise RuntimeError(f"Ollama API 返回成功，但模型仍存在: {selected.name}")

    store_after = _model_store_bytes(inventory.model_root)
    return OllamaModelDeleteResult(
        model=selected.name,
        digest=selected.digest,
        logical_model_bytes=selected.logical_bytes,
        store_before_bytes=store_before,
        store_after_bytes=store_after,
    )


def ollama_api_endpoint(environment: Mapping[str, str] | None = None) -> str:
    """Resolve a loopback-only Ollama API endpoint.

    DevClean intentionally refuses a remote ``OLLAMA_HOST``. Per-model deletion
    must never become a remote model-server administration feature.
    """

    env = _casefold_env(environment)
    raw = env.get("devclean_ollama_host") or env.get("ollama_host")
    if not raw:
        return _DEFAULT_ENDPOINT
    text = raw.strip()
    if "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError(f"只支持本机 HTTP Ollama API: {raw}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"Ollama API 地址包含不受支持的认证/参数: {raw}")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"OLLAMA_HOST 不应包含 API 路径: {raw}")

    host = parsed.hostname.casefold()
    if host in {"0.0.0.0", "localhost", "127.0.0.1"}:
        api_host = "127.0.0.1"
    elif host in {"::", "::1", "0:0:0:0:0:0:0:0", "0:0:0:0:0:0:0:1"}:
        api_host = "[::1]"
    else:
        raise ValueError(
            "拒绝连接非 loopback 的 OLLAMA_HOST；DevClean 只管理本机 Ollama 模型"
        )
    try:
        port = parsed.port or 11434
    except ValueError as error:
        raise ValueError(f"OLLAMA_HOST 端口无效: {raw}") from error
    return f"http://{api_host}:{port}"


def _json_request(
    endpoint: str,
    method: str,
    route: str,
    *,
    payload: Mapping[str, object] | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(dict(payload)).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        endpoint.rstrip("/") + route,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Ollama API {method} {route} 失败 (HTTP {error.code}): {detail}"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(
            f"无法连接本机 Ollama API {endpoint}: {error}"
        ) from error
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Ollama API {route} 返回了无效 JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Ollama API {route} 返回的顶层结构不是对象")
    return value


def _running_model_identities(payload: Mapping[str, Any]) -> set[str]:
    values = payload.get("models")
    if not isinstance(values, list):
        raise RuntimeError("Ollama /api/ps 缺少 models 数组")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in ("name", "model", "digest"):
            item = value.get(key)
            if isinstance(item, str) and item:
                result.add(item)
    return result


def _model_root(environment: Mapping[str, str] | None) -> Path | None:
    roots = ollama_roots(environment).model_roots
    if not roots:
        return None
    return Path(str(roots[0]))


def _model_store_bytes(root: Path | None) -> int | None:
    if root is None:
        return None
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    total = 0
    try:
        for directory, subdirs, files in os.walk(root, followlinks=False):
            base = Path(directory)
            safe_subdirs: list[str] = []
            for name in subdirs:
                child = base / name
                try:
                    if child.is_symlink() or child.is_junction():
                        continue
                except OSError:
                    continue
                safe_subdirs.append(name)
            subdirs[:] = safe_subdirs
            for name in files:
                path = base / name
                try:
                    if path.is_symlink():
                        continue
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _required_string(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RuntimeError(f"{label} 缺少 {key}")
    return item


def _optional_string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(key).casefold(): str(value) for key, value in source.items() if value}


__all__ = [
    "OllamaModelDeleteResult",
    "OllamaModelEntry",
    "OllamaModelInventory",
    "delete_ollama_model",
    "inventory_ollama_models",
    "ollama_api_endpoint",
]
