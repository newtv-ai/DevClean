from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
    RebuildCost,
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.huggingface_cleanup import (
    huggingface_audited_tool_roots,
    huggingface_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path, Path, Path]:
    home = tmp_path / "home"
    hf_home = home / ".cache" / "huggingface"
    hub = hf_home / "hub"
    xet = hf_home / "xet"
    assets = hf_home / "assets"
    token = hf_home / "token"
    for root in (hub, xet, assets):
        root.mkdir(parents=True)
    token.write_text("hf_secret", encoding="utf-8")
    env = {"USERPROFILE": str(home)}
    return env, hf_home, hub, xet, assets, token


def test_huggingface_default_roots_are_discovered(tmp_path: Path) -> None:
    env, hf_home, hub, xet, assets, token = _layout(tmp_path)

    roots = huggingface_roots(env)

    assert roots.home_roots == (PureWindowsPath(str(hf_home)),)
    assert roots.hub_cache_roots == (PureWindowsPath(str(hub)),)
    assert roots.xet_cache_roots == (PureWindowsPath(str(xet)),)
    assert roots.assets_cache_roots == (PureWindowsPath(str(assets)),)
    assert roots.token_paths == (PureWindowsPath(str(token)),)

    scan = application_scan_roots(env)
    for root in (hub, xet, assets):
        assert PureWindowsPath(str(root)) in scan


def test_huggingface_environment_overrides_are_honored(tmp_path: Path) -> None:
    env, _, _, _, _, _ = _layout(tmp_path)
    custom_home = tmp_path / "hf-home"
    hub = tmp_path / "hf-hub"
    xet = tmp_path / "hf-xet"
    assets = tmp_path / "hf-assets"
    token = tmp_path / "private" / "hf-token"
    env.update(
        {
            "HF_HOME": str(custom_home),
            "HF_HUB_CACHE": str(hub),
            "HF_XET_CACHE": str(xet),
            "HF_ASSETS_CACHE": str(assets),
            "HF_TOKEN_PATH": str(token),
        }
    )

    roots = huggingface_roots(env)

    assert roots.home_roots == (PureWindowsPath(str(custom_home)),)
    assert roots.hub_cache_roots == (PureWindowsPath(str(hub)),)
    assert roots.xet_cache_roots == (PureWindowsPath(str(xet)),)
    assert roots.assets_cache_roots == (PureWindowsPath(str(assets)),)
    assert roots.token_paths == (PureWindowsPath(str(token)),)


def test_huggingface_cache_and_token_rules_are_keep(tmp_path: Path) -> None:
    env, hf_home, hub, xet, assets, token = _layout(tmp_path)
    cases = {
        hub / "models--org--model" / "blobs" / "abc": (
            "huggingface-hub-cache-vendor-managed",
            RebuildCost.HIGH,
        ),
        xet / "shard-cache" / "shard": (
            "huggingface-xet-cache-vendor-managed",
            RebuildCost.MEDIUM,
        ),
        assets / "downstream" / "asset.bin": (
            "huggingface-assets-cache-vendor-managed",
            RebuildCost.MEDIUM,
        ),
        token: ("huggingface-auth-token", RebuildCost.HIGH),
        hf_home / "future-state" / "state.bin": (
            "huggingface-home-state",
            RebuildCost.HIGH,
        ),
    }

    for path, (rule_id, rebuild_cost) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert rule.rebuild_cost is rebuild_cost


def test_huggingface_generic_pipeline_keeps_old_large_cache(tmp_path: Path) -> None:
    env, _, hub, xet, assets, _ = _layout(tmp_path)

    for root in (hub, xet, assets):
        decision = evaluate_application_path(
            root,
            logical_size=64 * 1024**3,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_huggingface_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, _, hub, xet, assets, _ = _layout(tmp_path)

    assert huggingface_audited_tool_roots(env) == ()
    for root in (hub, xet, assets):
        assert whole_tree_application_rule(root, env) is None
        assert not application_cleanup.process_guard_allows(root, env)


def test_huggingface_cache_roots_are_catalogued_report_only(tmp_path: Path) -> None:
    env, _, hub, xet, assets, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    for root in (hub, xet, assets):
        item = by_path[os.path.normcase(str(root))]
        assert item.category is CleanupCategory.HUGGINGFACE_CACHE
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself
        assert item.application_rule is None


def test_huggingface_process_dispatch_is_independent_from_maven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        application_cleanup,
        "huggingface_process_running",
        lambda: True,
    )
    monkeypatch.setattr(application_cleanup, "maven_process_running", lambda: False)
    assert application_cleanup.application_process_running("huggingface")

    monkeypatch.setattr(
        application_cleanup,
        "huggingface_process_running",
        lambda: False,
    )
    monkeypatch.setattr(application_cleanup, "maven_process_running", lambda: True)
    assert not application_cleanup.application_process_running("huggingface")
