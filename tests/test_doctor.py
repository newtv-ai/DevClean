from __future__ import annotations

import pytest

from reclaimer.core.doctor import classify_execution_platform


@pytest.mark.parametrize(
    ("is_windows", "machine", "product_name", "status"),
    (
        (True, "AMD64", "Windows 11 Pro", "SUPPORTED_BASELINE"),
        (True, "x86_64", "Windows 10 Pro", "BEST_EFFORT_INVENTORY"),
        (True, "ARM64", "Windows 11 Pro", "UNSUPPORTED"),
        (True, "AMD64", None, "UNKNOWN"),
        (False, "x86_64", None, "UNSUPPORTED"),
    ),
)
def test_execution_platform_classification(
    is_windows: bool, machine: str, product_name: str | None, status: str
) -> None:
    result = classify_execution_platform(
        is_windows=is_windows,
        machine=machine,
        product_name=product_name,
    )

    assert result["status"] == status
    assert result["detail"]
