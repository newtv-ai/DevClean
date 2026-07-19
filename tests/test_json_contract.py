from __future__ import annotations

import pytest

from devclean.adapters.json_contract import strict_json_loads


def test_strict_json_rejects_duplicate_keys_and_nonstandard_constants() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        strict_json_loads('{"candidate": 1, "candidate": 2}')
    with pytest.raises(ValueError, match="not permitted"):
        strict_json_loads('{"size": NaN}')


def test_strict_json_accepts_standard_utf8_bytes() -> None:
    assert strict_json_loads(b'{"ok": true, "items": [1, 2]}') == {
        "ok": True,
        "items": [1, 2],
    }
