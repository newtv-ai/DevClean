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
from devclean.core.go_cleanup import go_audited_tool_roots, go_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    home = tmp_path / "home"
    local = tmp_path / "Local"
    appdata = tmp_path / "Roaming"
    build_cache = local / "go-build"
    module_cache = home / "go" / "pkg" / "mod"
    install_bin = home / "go" / "bin"
    goenv = appdata / "go" / "env"
    for root in (build_cache, module_cache, install_bin):
        root.mkdir(parents=True)
    goenv.parent.mkdir(parents=True)
    goenv.write_text("GOPROXY=https://proxy.golang.org,direct\n", encoding="utf-8")
    env = {
        "USERPROFILE": str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(appdata),
        "GOPATH": str(home / "go"),
    }
    return env, build_cache, module_cache, install_bin, goenv


def test_go_default_windows_storage_roots_are_discovered(tmp_path: Path) -> None:
    env, build_cache, module_cache, install_bin, goenv = _layout(tmp_path)

    roots = go_roots(env)

    assert roots.build_cache_roots == (PureWindowsPath(str(build_cache)),)
    assert roots.module_cache_roots == (PureWindowsPath(str(module_cache)),)
    assert roots.install_bin_roots == (PureWindowsPath(str(install_bin)),)
    assert roots.config_paths == (PureWindowsPath(str(goenv)),)

    scan = application_scan_roots(env)
    assert PureWindowsPath(str(build_cache)) in scan
    assert PureWindowsPath(str(module_cache)) in scan
    assert PureWindowsPath(str(install_bin)) not in scan


def test_go_environment_overrides_replace_default_cache_roots(tmp_path: Path) -> None:
    env, default_build, default_module, _, _ = _layout(tmp_path)
    custom_build = tmp_path / "cache-drive" / "go-build"
    custom_module = tmp_path / "cache-drive" / "go-mod"
    env["GOCACHE"] = str(custom_build)
    env["GOMODCACHE"] = str(custom_module)

    roots = go_roots(env)

    assert roots.build_cache_roots == (PureWindowsPath(str(custom_build)),)
    assert roots.module_cache_roots == (PureWindowsPath(str(custom_module)),)
    assert PureWindowsPath(str(default_build)) not in roots.build_cache_roots
    assert PureWindowsPath(str(default_module)) not in roots.module_cache_roots


def test_go_cache_and_persistent_state_are_protected(tmp_path: Path) -> None:
    env, build_cache, module_cache, install_bin, goenv = _layout(tmp_path)
    cases = {
        build_cache / "00" / "artifact-a": (
            "go-build-cache-vendor-managed",
            RebuildCost.LOW,
        ),
        module_cache / "example.com" / "lib@v1.2.3" / "lib.go": (
            "go-module-cache-vendor-managed",
            RebuildCost.HIGH,
        ),
        install_bin / "stringer.exe": ("go-installed-binaries", RebuildCost.HIGH),
        goenv: ("go-environment-configuration", RebuildCost.HIGH),
    }

    for path, (rule_id, rebuild_cost) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert rule.rebuild_cost is rebuild_cost


def test_go_project_module_metadata_is_protected_anywhere(tmp_path: Path) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    project = tmp_path / "work" / "service"

    for name in ("go.mod", "go.sum", "go.work", "go.work.sum"):
        rule = match_application_rule(project / name, env)
        assert rule is not None
        assert rule.rule_id == "go-project-module-metadata"
        assert rule.owner is DecisionOwner.KEEP


def test_go_generic_pipeline_keeps_old_large_caches(tmp_path: Path) -> None:
    env, build_cache, module_cache, _, _ = _layout(tmp_path)

    for root in (build_cache, module_cache):
        decision = evaluate_application_path(
            root,
            logical_size=16 * 1024**3,
            last_used=_NOW - timedelta(days=365),
            now=_NOW,
            process_running=False,
            environment=env,
        )
        assert decision is not None
        assert decision.action is PolicyAction.KEEP_PROTECTED


def test_go_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, build_cache, module_cache, _, _ = _layout(tmp_path)

    assert go_audited_tool_roots(env) == ()
    for root in (build_cache, module_cache):
        assert whole_tree_application_rule(root, env) is None
        assert not application_cleanup.process_guard_allows(root, env)


def test_go_cache_roots_are_catalogued_report_only(tmp_path: Path) -> None:
    env, build_cache, module_cache, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    for root in (build_cache, module_cache):
        item = by_path[os.path.normcase(str(root))]
        assert item.category is CleanupCategory.GO_MODULE_CACHE
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself
        assert item.application_rule is None


def test_go_process_dispatch_is_independent_from_nuget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "go_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "nuget_process_running", lambda: False)
    assert application_cleanup.application_process_running("go")

    monkeypatch.setattr(application_cleanup, "go_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "nuget_process_running", lambda: True)
    assert not application_cleanup.application_process_running("go")
