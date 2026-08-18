from __future__ import annotations

import pytest

import devclean.platform.windows.registry as registry
from devclean.platform.windows.registry import RegistryStringLookup, RegistryStringValue


def test_first_hklm_string_value_respects_declared_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    values = {
        "policy": RegistryStringLookup(None, True),
        "machine": RegistryStringLookup(
            RegistryStringValue(r"D:\VSCache", 1, "machine"),
            True,
        ),
        "legacy": RegistryStringLookup(
            RegistryStringValue(r"E:\WrongCache", 1, "legacy"),
            True,
        ),
    }

    def query(key_path: str, value_name: str) -> RegistryStringLookup:
        calls.append((key_path, value_name))
        return values[key_path]

    monkeypatch.setattr(registry, "query_hklm_string_value", query)

    result = registry.first_hklm_string_value(
        ("policy", "machine", "legacy"),
        "CachePath",
    )

    assert result.value is not None
    assert result.value.value == r"D:\VSCache"
    assert calls == [("policy", "CachePath"), ("machine", "CachePath")]


def test_first_hklm_string_value_stops_on_inconclusive_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def query(key_path: str, value_name: str) -> RegistryStringLookup:
        del value_name
        calls.append(key_path)
        if key_path == "policy":
            return RegistryStringLookup(None, False)
        return RegistryStringLookup(
            RegistryStringValue(r"D:\MustNotBeUsed", 1, key_path),
            True,
        )

    monkeypatch.setattr(registry, "query_hklm_string_value", query)

    result = registry.first_hklm_string_value(
        ("policy", "machine", "legacy"),
        "CachePath",
    )

    assert not result.conclusive
    assert result.value is None
    assert calls == ["policy"]


def test_first_hklm_string_value_reports_conclusive_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry,
        "query_hklm_string_value",
        lambda *_args: RegistryStringLookup(None, True),
    )

    result = registry.first_hklm_string_value(("one", "two"), "CachePath")

    assert result.conclusive
    assert result.value is None
