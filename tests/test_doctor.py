from __future__ import annotations

import pytest

from devclean.core.doctor import classify_execution_platform


@pytest.mark.parametrize(
    ("is_windows", "machine", "product_name", "build_number", "status"),
    (
        (True, "AMD64", "Windows 11 Pro", 22631, "SUPPORTED_BASELINE"),
        (True, "AMD64", "Windows 10 Pro", 26200, "SUPPORTED_BASELINE"),
        (True, "x86_64", "Windows 10 Pro", 19045, "BEST_EFFORT_INVENTORY"),
        (True, "ARM64", "Windows 11 Pro", 22631, "UNSUPPORTED"),
        (True, "AMD64", None, None, "UNKNOWN"),
        (False, "x86_64", None, None, "UNSUPPORTED"),
    ),
)
def test_execution_platform_classification(
    is_windows: bool,
    machine: str,
    product_name: str | None,
    build_number: int | None,
    status: str,
) -> None:
    result = classify_execution_platform(
        is_windows=is_windows,
        machine=machine,
        product_name=product_name,
        build_number=build_number,
    )

    assert result["status"] == status
    assert result["detail"]
