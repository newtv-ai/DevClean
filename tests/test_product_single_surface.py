from __future__ import annotations

import inspect

from devclean.ui import launcher
from devclean.ui.product_vendor_app import ProductDevCleanWindow


def test_launcher_exposes_only_the_product_window() -> None:
    assert launcher.ProductDevCleanWindow is ProductDevCleanWindow
    assert not hasattr(launcher, "_install_tools_menu")


def test_product_header_has_no_manual_tool_center() -> None:
    source = inspect.getsource(ProductDevCleanWindow._build_header)

    assert "工具中心" not in source
    assert "DevCleanOpenTools" not in source
