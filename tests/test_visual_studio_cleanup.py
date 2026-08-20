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
    audited_dynamic_tool_roots,
    evaluate_application_path,
    match_application_rule,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import default_rules
from devclean.core.visual_studio_cleanup import visual_studio_roots

_NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    local = tmp_path / "LocalAppData"
    parent = local / "Microsoft" / "VisualStudio"
    vs2022 = parent / "17.0_deadbeef"
    vs2026 = parent / "18.0_cafebabe"
    cache2022 = vs2022 / "ComponentModelCache"
    cache2026 = vs2026 / "ComponentModelCache"
    cache2022.mkdir(parents=True)
    cache2026.mkdir(parents=True)
    (vs2022 / "WebTools").mkdir()
    (vs2026 / "WebTools").mkdir()
    (parent / "Roslyn" / "Cache").mkdir(parents=True)
    (vs2022 / "ImageLibrary").mkdir()
    env = {"LOCALAPPDATA": str(local)}
    return env, cache2022, cache2026, vs2022


def _roslyn_cache(environment: dict[str, str]) -> Path:
    return Path(environment["LOCALAPPDATA"]) / "Microsoft" / "VisualStudio" / "Roslyn" / "Cache"


def _web_tools(environment: dict[str, str], selector: str = "17.0_deadbeef") -> Path:
    return Path(environment["LOCALAPPDATA"]) / "Microsoft" / "VisualStudio" / selector / "WebTools"


def test_visual_studio_component_caches_are_discovered(tmp_path: Path) -> None:
    env, cache2022, cache2026, _ = _layout(tmp_path)

    roots = visual_studio_roots(env)

    assert roots.component_model_cache_roots == (
        PureWindowsPath(str(cache2022)),
        PureWindowsPath(str(cache2026)),
    )
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(cache2022)) in scan
    assert PureWindowsPath(str(cache2026)) in scan


def test_visual_studio_roslyn_cache_is_discovered(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    roots = visual_studio_roots(env)

    assert roots.roslyn_cache_roots == (PureWindowsPath(str(roslyn)),)
    assert PureWindowsPath(str(roslyn)) in application_scan_roots(env)


def test_visual_studio_webtools_roots_are_discovered_report_only(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    web2022 = _web_tools(env)
    web2026 = _web_tools(env, "18.0_cafebabe")

    roots = visual_studio_roots(env)

    assert roots.web_tools_roots == (
        PureWindowsPath(str(web2022)),
        PureWindowsPath(str(web2026)),
    )
    scan = application_scan_roots(env)
    assert PureWindowsPath(str(web2022)) in scan
    assert PureWindowsPath(str(web2026)) in scan


def test_visual_studio_only_claims_component_model_cache(tmp_path: Path) -> None:
    env, cache2022, _, instance = _layout(tmp_path)

    rule = match_application_rule(cache2022 / "Microsoft.VisualStudio.Default.cache", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-component-model-cache"
    assert rule.owner is DecisionOwner.TOOL
    assert match_application_rule(instance / "ImageLibrary" / "state.dat", env) is None
    assert match_application_rule(instance / "privateregistry.bin", env) is None


def test_visual_studio_only_claims_exact_roslyn_cache_subtree(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    rule = match_application_rule(roslyn / "analyzer-cache.bin", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-roslyn-analyzer-cache"
    assert rule.owner is DecisionOwner.TOOL
    assert match_application_rule(roslyn.parent / "state.json", env) is None


def test_visual_studio_webtools_is_explicitly_protected(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    web_tools = _web_tools(env)

    rule = match_application_rule(web_tools / "LanguageService" / "state.db", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-webtools-mixed-state"
    assert rule.owner is DecisionOwner.KEEP


def test_visual_studio_old_large_component_cache_is_delegated(tmp_path: Path) -> None:
    env, cache2022, _, _ = _layout(tmp_path)

    decision = evaluate_application_path(
        cache2022,
        logical_size=512 * 1024**2,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_DELETE


def test_visual_studio_old_large_roslyn_cache_is_delegated(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    decision = evaluate_application_path(
        roslyn,
        logical_size=512 * 1024**2,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_DELETE


def test_visual_studio_old_large_webtools_stays_protected(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    web_tools = _web_tools(env)

    decision = evaluate_application_path(
        web_tools,
        logical_size=12 * 1024**3,
        last_used=_NOW - timedelta(days=730),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert not application_cleanup.process_guard_allows(web_tools, env)


def test_visual_studio_component_cache_stays_when_devenv_is_running(
    tmp_path: Path,
) -> None:
    env, cache2022, _, _ = _layout(tmp_path)

    decision = evaluate_application_path(
        cache2022,
        logical_size=512 * 1024**2,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_visual_studio_roslyn_cache_stays_when_devenv_is_running(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    decision = evaluate_application_path(
        roslyn,
        logical_size=512 * 1024**2,
        last_used=_NOW - timedelta(days=90),
        now=_NOW,
        process_running=True,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.TOOL_KEEP_IN_USE


def test_visual_studio_component_cache_has_exact_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, cache2022, _, instance = _layout(tmp_path)

    dynamic = dict(audited_dynamic_tool_roots(env))
    assert PureWindowsPath(str(cache2022)) in dynamic
    rule = whole_tree_application_rule(cache2022, env)
    assert rule is not None
    assert rule.rule_id == "visual-studio-component-model-cache"
    assert whole_tree_application_rule(instance, env) is None


def test_visual_studio_roslyn_cache_has_exact_whole_tree_authority(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    dynamic = dict(audited_dynamic_tool_roots(env))
    assert PureWindowsPath(str(roslyn)) in dynamic
    rule = whole_tree_application_rule(roslyn, env)
    assert rule is not None
    assert rule.rule_id == "visual-studio-roslyn-analyzer-cache"
    assert whole_tree_application_rule(roslyn.parent, env) is None


def test_visual_studio_webtools_never_gets_whole_tree_authority(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    web_tools = _web_tools(env)

    dynamic = dict(audited_dynamic_tool_roots(env))
    assert PureWindowsPath(str(web_tools)) not in dynamic
    assert whole_tree_application_rule(web_tools, env) is None


def test_visual_studio_component_cache_is_catalogued_vendor_managed(
    tmp_path: Path,
) -> None:
    env, cache2022, cache2026, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    for cache in (cache2022, cache2026):
        item = by_path[os.path.normcase(str(cache))]
        assert item.category is CleanupCategory.IDE_CACHE
        assert item.policy is CleanupPolicy.VENDOR_MANAGED
        assert item.delete_root_itself
        assert item.application_rule is not None
        assert item.application_rule.rule_id == "visual-studio-component-model-cache"


def test_visual_studio_roslyn_cache_is_catalogued_vendor_managed(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    roslyn = _roslyn_cache(env)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(roslyn))]

    assert item.category is CleanupCategory.IDE_CACHE
    assert item.policy is CleanupPolicy.VENDOR_MANAGED
    assert item.delete_root_itself
    assert item.application_rule is not None
    assert item.application_rule.rule_id == "visual-studio-roslyn-analyzer-cache"


def test_visual_studio_webtools_is_catalogued_report_only(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    web_tools = _web_tools(env)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(web_tools))]

    assert item.category is CleanupCategory.IDE_CACHE
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_visual_studio_does_not_claim_project_outputs(tmp_path: Path) -> None:
    env, _, _, _ = _layout(tmp_path)
    project = tmp_path / "src" / "sample"

    assert match_application_rule(project / ".vs" / "config" / "state.json", env) is None
    assert match_application_rule(project / "bin" / "Debug" / "app.exe", env) is None
    assert match_application_rule(project / "obj" / "Debug" / "app.obj", env) is None


def test_visual_studio_process_dispatch_does_not_alias_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "visual_studio_process_running", lambda: True)
    monkeypatch.setattr(
        application_cleanup,
        "visual_studio_installer_process_running",
        lambda: False,
    )
    assert application_cleanup.application_process_running("visual_studio")

    monkeypatch.setattr(application_cleanup, "visual_studio_process_running", lambda: False)
    monkeypatch.setattr(
        application_cleanup,
        "visual_studio_installer_process_running",
        lambda: True,
    )
    assert not application_cleanup.application_process_running("visual_studio")
