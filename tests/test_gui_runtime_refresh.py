from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from devclean.core.application_cleanup import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)
from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.user_rules import UserRules, default_rules
from devclean.platform.windows import subprocess_policy
from devclean.ui.modern_app import automatic_cleanup_roots, smart_scan_targets


def _audited_rule() -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id="test-tool-cache",
        app_id="test",
        root_key="TEST",
        relative_pattern="cache",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.LOW,
        idle_days=7,
        allow_whole_tree=True,
        label="Test tool cache",
    )


def _known_root(path: Path, rule: ApplicationCleanupRule) -> KnownCleanupRoot:
    return KnownCleanupRoot(
        path=path,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="actionable",
        delete_root_itself=True,
        application_rule=rule,
    )


def test_gui_subprocess_policy_hides_normal_console_children() -> None:
    hidden = subprocess_policy._hidden_console_creationflags(0)
    assert hidden & 0x08000000


def test_gui_subprocess_policy_respects_explicit_console_requests() -> None:
    create_new_console = 0x00000010
    detached_process = 0x00000008
    assert subprocess_policy._hidden_console_creationflags(create_new_console) == create_new_console
    assert subprocess_policy._hidden_console_creationflags(detached_process) == detached_process


def test_smart_scan_uses_audited_actionable_roots_not_profile_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "profile"
    actionable = profile / ".tool" / "cache"
    report_only = profile / ".tool" / "models"
    actionable.mkdir(parents=True)
    report_only.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))

    trusted = _known_root(actionable, _audited_rule())
    inventory = KnownCleanupRoot(
        path=report_only,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.REPORT_ONLY,
        label="inventory",
    )
    rules = default_rules()

    roots = smart_scan_targets((trusted, inventory), (), rules)

    assert actionable.resolve() in roots
    assert report_only.resolve() not in roots
    assert profile.resolve() not in roots


def test_smart_scan_keeps_explicit_additional_path(tmp_path: Path) -> None:
    explicit = tmp_path / "project-cache"
    explicit.mkdir()
    base = default_rules()
    rules = UserRules(
        scan=replace(base.scan, additional_paths=(str(explicit),)),
        delete=base.delete,
        keep=base.keep,
    )

    roots = smart_scan_targets((), (), rules)

    assert explicit.resolve() in roots


def test_automatic_cleanup_skips_high_rebuild_cost_roots(tmp_path: Path) -> None:
    root = tmp_path / "index"
    root.mkdir()
    rule = replace(_audited_rule(), rebuild_cost=RebuildCost.HIGH)

    assert automatic_cleanup_roots((_known_root(root, rule),)) == ()


def test_automatic_cleanup_skips_cache_while_owner_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.ui.modern_app as modern_app

    root = tmp_path / "cache"
    root.mkdir()
    rule = replace(_audited_rule(), requires_process_closed=True)
    monkeypatch.setattr(modern_app, "clear_process_cache", lambda: None)
    monkeypatch.setattr(modern_app, "application_process_running", lambda _app_id: True)

    assert modern_app.automatic_cleanup_roots((_known_root(root, rule),)) == ()


def test_automatic_cleanup_includes_closed_regenerable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.ui.modern_app as modern_app

    root = tmp_path / "cache"
    root.mkdir()
    rule = replace(_audited_rule(), requires_process_closed=True)
    known = _known_root(root, rule)
    monkeypatch.setattr(modern_app, "clear_process_cache", lambda: None)
    monkeypatch.setattr(modern_app, "application_process_running", lambda _app_id: False)

    assert modern_app.automatic_cleanup_roots((known,)) == (known,)
