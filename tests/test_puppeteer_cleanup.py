from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import (
    DecisionOwner,
    PolicyAction,
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
from devclean.core.puppeteer_cleanup import (
    puppeteer_audited_tool_roots,
    puppeteer_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    cache = home / ".cache" / "puppeteer"
    cache.mkdir(parents=True)
    env = {"USERPROFILE": str(home)}
    return env, cache


def test_puppeteer_default_cache_is_discovered(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)

    roots = puppeteer_roots(env)

    assert roots.browser_cache_roots == (PureWindowsPath(str(cache)),)
    assert not roots.relative_cache_override
    assert PureWindowsPath(str(cache)) in application_scan_roots(env)


def test_puppeteer_absolute_cache_override_replaces_default(tmp_path: Path) -> None:
    env, default_cache = _layout(tmp_path)
    custom = tmp_path / "browser-drive" / "puppeteer"
    custom.mkdir(parents=True)
    env["PUPPETEER_CACHE_DIR"] = str(custom)

    roots = puppeteer_roots(env)

    assert roots.browser_cache_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(default_cache)) not in roots.browser_cache_roots


def test_puppeteer_relative_override_is_fail_closed(tmp_path: Path) -> None:
    env, _ = _layout(tmp_path)
    env["PUPPETEER_CACHE_DIR"] = "relative-browser-cache"

    roots = puppeteer_roots(env)

    assert roots.browser_cache_roots == ()
    assert roots.relative_cache_override


def test_puppeteer_cache_is_keep_even_when_large_and_old(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)
    browser = cache / "chrome" / "win64-151.0.7922.47" / "chrome.exe"

    rule = match_application_rule(browser, env)
    assert rule is not None
    assert rule.rule_id == "puppeteer-browser-cache-vendor-managed"
    assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        cache,
        logical_size=20 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_puppeteer_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)

    assert puppeteer_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_puppeteer_cache_is_catalogued_report_only(tmp_path: Path) -> None:
    env, cache = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(cache))]

    assert item.category is CleanupCategory.TEST_BROWSER_BINARIES
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_puppeteer_process_dispatch_does_not_alias_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "puppeteer_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "playwright_process_running", lambda: False)
    assert application_cleanup.application_process_running("puppeteer")

    monkeypatch.setattr(application_cleanup, "puppeteer_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "playwright_process_running", lambda: True)
    assert not application_cleanup.application_process_running("puppeteer")
