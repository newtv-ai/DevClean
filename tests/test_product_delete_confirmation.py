from __future__ import annotations

import threading
from tkinter import messagebox
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from devclean.ui import product_vendor_app as product


@pytest.mark.parametrize("accepted", [False, True])
def test_permanent_cleanup_starts_only_after_explicit_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    selected = (object(), object())
    window = SimpleNamespace(
        _selected_items=lambda: selected,
        _selected_vendor_candidates=lambda: (),
        _busy=None,
        _sync_buttons=Mock(),
        _progress=Mock(),
        _status=Mock(),
        _product_delete_worker=Mock(),
    )
    confirm = Mock(return_value=accepted)
    thread = Mock()
    monkeypatch.setattr(messagebox, "askyesno", confirm)
    monkeypatch.setattr(threading, "Thread", thread)
    product.ProductDevCleanWindow._delete(
        cast(product.ProductDevCleanWindow, window),
        irreversible=True,
    )
    assert "2 项" in confirm.call_args.args[1]
    assert "不进入回收站" in confirm.call_args.args[1]
    assert confirm.call_args.kwargs["default"] == messagebox.NO
    if accepted:
        thread.return_value.start.assert_called_once()
        assert window._busy == "deleting"
    else:
        thread.assert_not_called()
        window._sync_buttons.assert_not_called()
        assert window._busy is None
