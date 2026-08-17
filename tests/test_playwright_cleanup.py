from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

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
from devclean.core.playwright_cleanup import (
    playwright_audited_tool_roots,
    playwright_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    local = tmp_path / "local"
    registry = local / "ms-playwright"
    registry.mkdir(parents=True)
    env = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
    }
    return env, registry


def test_playwright_default_windows_registry_is_discovered(tmp_path: Path) -> None:
    env, registry = _layout(tmp_path)

    roots = playwright_roots(env)

    assert roots.browser_registry_roots == (PureWindowsPath(str(registry)),)
    assert not roots.project_local_browsers
    assert not roots.browser_gc_disabled
    assert PureWindowsPath(str(registry)) in application_scan_roots(env)


def test_playwright_absolute_browser_path_override_replaces_default(
    tmp_path: Path,
) -> None:
    env, default_registry = _layout(tmp_path)
    custom = tmp_path / "browser-drive" / "playwright"
    custom.mkdir(parents=True)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(custom)

    roots = playwright_roots(env)

    assert roots.browser_registry_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(default_registry)) not in roots.browser_registry_roots


def test_playwright_project_local_mode_is_fail_closed(tmp_path: Path) -> None:
    env, _ = _layout(tmp_path)
    env["PLAYWRIGHT_BROWSERS_PATH"] = "0"

    roots = playwright_roots(env)

    assert roots.browser_registry_roots == ()
    assert roots.project_local_browsers
    assert application_scan_roots(env) == tuple(
        root for root in application_scan_roots(env) if "ms-playwright" not in str(root)
    )


def test_playwright_relative_override_is_not_guessed(tmp_path: Path) -> None:
    env, _ = _layout(tmp_path)
    env["PLAYWRIGHT_BROWSERS_PATH"] = "relative-browser-cache"

    roots = playwright_roots(env)

    assert roots.browser_registry_roots == ()


def test_playwright_registry_is_keep_even_when_large_and_old(tmp_path: Path) -> None:
    env, registry = _layout(tmp_path)
    browser = registry / "chromium-1234" / "chrome-win" / "chrome.exe"

    rule = match_application_rule(browser, env)
    assert rule is not None
    assert rule.rule_id == "playwright-browser-registry-vendor-managed"
    assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        registry,
        logical_size=20 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_playwright_skip_gc_is_recorded_without_granting_delete_authority(
    tmp_path: Path,
) -> None:
    env, registry = _layout(tmp_path)
    env["PLAYWRIGHT_SKIP_BROWSER_GC"] = "1"

    roots = playwright_roots(env)

    assert roots.browser_gc_disabled
    assert playwright_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(registry, env) is None
    assert not application_cleanup.process_guard_allows(registry, env)


def test_playwright_registry_is_catalogued_report_only(tmp_path: Path) -> None:
    env, registry = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(registry))]

    assert item.category is CleanupCategory.TEST_BROWSER_BINARIES
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_playwright_process_dispatch_does_not_alias_docker(monkeypatch) -> None:
    monkeypatch.setattr(application_cleanup, "playwright_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "docker_process_running", lambda: False)
    assert application_cleanup.application_process_running("playwright")

    monkeypatch.setattr(application_cleanup, "playwright_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "docker_process_running", lambda: True)
    assert not application_cleanup.application_process_running("playwright")
