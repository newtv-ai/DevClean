from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import devclean.ui.product_vendor_app as product
from devclean.core.user_rules import default_rules
from devclean.scanner import CancellationToken


def _window(tmp_path: Path) -> product.ProductDevCleanWindow:
    window = object.__new__(product.ProductDevCleanWindow)
    window._active_scan_drives = (Path(tmp_path.anchor),)
    window._vendor_scan_candidates = {}
    return window


def _capture_base_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_base_worker(
        self: object,
        token: str,
        roots: tuple[Path, ...],
        cancel: CancellationToken,
        active_rules: object,
        known_roots: object,
    ) -> None:
        del self, token, roots, cancel, known_roots
        captured["rules"] = active_rules

    monkeypatch.setattr(product._BaseProductDevCleanWindow, "_scan_worker", fake_base_worker)
    monkeypatch.setattr(
        product,
        "inventory_vendor_cleanup_candidates",
        lambda **_kwargs: SimpleNamespace(candidates=()),
    )
    return captured


def test_product_worker_wires_source_proven_npm_paths_into_scan_exclusions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(tmp_path)
    captured = _capture_base_worker(monkeypatch)
    npm_child = tmp_path / "npm-cache" / "_cacache"
    npm_child.mkdir(parents=True)
    monkeypatch.setattr(product, "npm_generic_scan_skip_paths", lambda: (npm_child,))
    rules = default_rules()

    window._scan_worker(
        "token",
        (tmp_path,),
        CancellationToken(),
        rules,
        (),
    )

    effective = captured["rules"]
    assert hasattr(effective, "scan")
    assert str(npm_child) in effective.scan.excluded_paths


def test_product_worker_falls_back_to_full_scan_when_npm_proof_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _window(tmp_path)
    captured = _capture_base_worker(monkeypatch)

    def fail_npm_proof() -> tuple[Path, ...]:
        raise RuntimeError("unproven npm boundary")

    monkeypatch.setattr(product, "npm_generic_scan_skip_paths", fail_npm_proof)
    rules = default_rules()

    window._scan_worker(
        "token",
        (tmp_path,),
        CancellationToken(),
        rules,
        (),
    )

    effective = captured["rules"]
    assert hasattr(effective, "scan")
    assert effective.scan.excluded_paths == rules.scan.excluded_paths
