"""Strict JSON decoding shared by untrusted vendor and manifest adapters."""

from __future__ import annotations

import json
from typing import Any, NoReturn


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Decode JSON while rejecting duplicate keys and non-standard numeric constants."""

    return json.loads(
        value,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON constant {value!r} is not permitted")


__all__ = ["strict_json_loads"]
