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
from devclean.core.nuget_cleanup import nuget_audited_tool_roots, nuget_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    home = tmp_path / "home"
    local = tmp_path / "Local"
    temp = tmp_path / "Temp"
    appdata = tmp_path / "Roaming"
    global_packages = home / ".nuget" / "packages"
    http_cache = local / "NuGet" / "v3-cache"
    scratch = temp / "NuGetScratch"
    plugins = local / "NuGet" / "plugins-cache"
    for root in (global_packages, http_cache, scratch, plugins):
        root.mkdir(parents=True)
    config = appdata / "NuGet" / "NuGet.Config"
    config.parent.mkdir(parents=True)
    config.write_text("<configuration />", encoding="utf-8")
    env = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(appdata),
        "TEMP": str(temp),
    }
    return env, global_packages, http_cache, scratch, plugins


def test_nuget_windows_local_resource_roots_are_discovered(tmp_path: Path) -> None:
    env, global_packages, http_cache, scratch, plugins = _layout(tmp_path)

    roots = nuget_roots(env)

    assert PureWindowsPath(str(global_packages)) in roots.global_packages_roots
    assert PureWindowsPath(str(http_cache)) in roots.http_cache_roots
    assert PureWindowsPath(str(scratch)) in roots.temp_roots
    assert PureWindowsPath(str(plugins)) in roots.plugins_cache_roots

    scan = application_scan_roots(env)
    for root in (global_packages, http_cache, scratch, plugins):
        assert PureWindowsPath(str(root)) in scan


def test_nuget_environment_overrides_replace_default_roots(tmp_path: Path) -> None:
    env, default_packages, default_http, default_temp, default_plugins = _layout(tmp_path)
    custom_packages = tmp_path / "cache" / "packages"
    custom_http = tmp_path / "cache" / "http"
    custom_temp = tmp_path / "cache" / "scratch"
    custom_plugins = tmp_path / "cache" / "plugins"
    env.update(
        {
            "NUGET_PACKAGES": str(custom_packages),
            "NUGET_HTTP_CACHE_PATH": str(custom_http),
            "NUGET_SCRATCH": str(custom_temp),
            "NUGET_PLUGINS_CACHE_PATH": str(custom_plugins),
        }
    )

    roots = nuget_roots(env)

    assert roots.global_packages_roots == (PureWindowsPath(str(custom_packages)),)
    assert roots.http_cache_roots == (PureWindowsPath(str(custom_http)),)
    assert roots.temp_roots == (PureWindowsPath(str(custom_temp)),)
    assert roots.plugins_cache_roots == (PureWindowsPath(str(custom_plugins)),)
    assert PureWindowsPath(str(default_packages)) not in roots.global_packages_roots
    assert PureWindowsPath(str(default_http)) not in roots.http_cache_roots
    assert PureWindowsPath(str(default_temp)) not in roots.temp_roots
    assert PureWindowsPath(str(default_plugins)) not in roots.plugins_cache_roots


def test_nuget_local_resources_are_vendor_managed_keep(tmp_path: Path) -> None:
    env, global_packages, http_cache, scratch, plugins = _layout(tmp_path)
    cases = {
        global_packages / "newtonsoft.json" / "13.0.3" / "lib.dll": (
            "nuget-global-packages-vendor-managed",
            RebuildCost.HIGH,
        ),
        http_cache / "index.dat": ("nuget-http-cache-vendor-managed", RebuildCost.LOW),
        scratch / "restore.tmp": ("nuget-temp-vendor-managed", RebuildCost.NONE),
        plugins / "credential-provider.json": (
            "nuget-plugins-cache-vendor-managed",
            RebuildCost.LOW,
        ),
    }

    for path, (rule_id, rebuild_cost) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert rule.rebuild_cost is rebuild_cost


def test_nuget_project_metadata_and_config_are_protected(tmp_path: Path) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    project = tmp_path / "work" / "app"
    for name in (
        "NuGet.Config",
        "packages.config",
        "packages.lock.json",
        "Directory.Packages.props",
    ):
        rule = match_application_rule(project / name, env)
        assert rule is not None
        assert rule.owner is DecisionOwner.KEEP

    user_config = Path(env["APPDATA"]) / "NuGet" / "NuGet.Config"
    rule = match_application_rule(user_config, env)
    assert rule is not None
    assert rule.rule_id in {"nuget-configuration", "nuget-project-metadata"}
    assert rule.owner is DecisionOwner.KEEP


def test_nuget_generic_pipeline_keeps_even_very_old_large_cache(tmp_path: Path) -> None:
    env, _, http_cache, _, _ = _layout(tmp_path)

    decision = evaluate_application_path(
        http_cache,
        logical_size=8 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_nuget_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, global_packages, http_cache, scratch, plugins = _layout(tmp_path)

    assert nuget_audited_tool_roots(env) == ()
    for root in (global_packages, http_cache, scratch, plugins):
        assert whole_tree_application_rule(root, env) is None
        assert not application_cleanup.process_guard_allows(root, env)


def test_nuget_roots_are_catalogued_report_only(tmp_path: Path) -> None:
    env, global_packages, http_cache, scratch, plugins = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    for root in (global_packages, http_cache, scratch, plugins):
        item = by_path[os.path.normcase(str(root))]
        assert item.category is CleanupCategory.NUGET_CACHE
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself
        assert item.application_rule is None


def test_nuget_process_dispatch_is_independent_from_conda(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "nuget_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "conda_process_running", lambda: False)
    assert application_cleanup.application_process_running("nuget")

    monkeypatch.setattr(application_cleanup, "nuget_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "conda_process_running", lambda: True)
    assert not application_cleanup.application_process_running("nuget")
