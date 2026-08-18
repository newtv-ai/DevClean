from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

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


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path]:
    local = tmp_path / "LocalAppData"
    packages = local / "Microsoft" / "VisualStudio" / "Packages"
    channels = packages / "_channels"
    channels.mkdir(parents=True)
    (channels / "channelManifest.json").write_text("{}", encoding="utf-8")
    return {"LOCALAPPDATA": str(local)}, packages


def test_visual_studio_local_packages_root_is_discovered(tmp_path: Path) -> None:
    env, packages = _layout(tmp_path)

    roots = visual_studio_roots(env)

    assert roots.local_package_roots == (PureWindowsPath(str(packages)),)
    assert PureWindowsPath(str(packages)) in application_scan_roots(env)


def test_visual_studio_local_packages_are_explicitly_protected(tmp_path: Path) -> None:
    env, packages = _layout(tmp_path)

    rule = match_application_rule(packages / "_channels" / "channelManifest.json", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-local-packages-servicing-state"
    assert rule.owner is DecisionOwner.KEEP


def test_visual_studio_old_large_local_packages_stay_protected(tmp_path: Path) -> None:
    env, packages = _layout(tmp_path)

    decision = evaluate_application_path(
        packages,
        logical_size=20 * 1024**3,
        last_used=_NOW - timedelta(days=730),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED
    assert not application_cleanup.process_guard_allows(packages, env)


def test_visual_studio_local_packages_never_get_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, packages = _layout(tmp_path)

    dynamic = dict(audited_dynamic_tool_roots(env))

    assert PureWindowsPath(str(packages)) not in dynamic
    assert whole_tree_application_rule(packages, env) is None


def test_visual_studio_local_packages_are_catalogued_report_only(
    tmp_path: Path,
) -> None:
    env, packages = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(packages))]

    assert item.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None
