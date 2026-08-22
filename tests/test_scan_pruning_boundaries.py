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
from devclean.core.user_rules import UserRules, default_rules, normalise_path
from devclean.scanner.filesystem import ScanOptions, scan_roots
from devclean.ui import app


def _rules_with_additional(path: Path, *, include_user_profile: bool = False) -> UserRules:
    base = default_rules()
    return UserRules(
        scan=replace(
            base.scan,
            additional_paths=(str(path),),
            include_user_profile=include_user_profile,
        ),
        delete=base.delete,
        keep=base.keep,
    )


def _audited_rule() -> ApplicationCleanupRule:
    return ApplicationCleanupRule(
        rule_id="test-audited-cache",
        app_id="test",
        root_key="TEST",
        relative_pattern="cache",
        match_kind=MatchKind.PREFIX,
        owner=DecisionOwner.TOOL,
        last_use=LastUseStrategy.FILE_MTIME,
        rebuild_cost=RebuildCost.LOW,
        idle_days=7,
        allow_whole_tree=True,
        label="Test audited cache",
    )


def test_explicit_root_can_bypass_only_its_own_skipped_basename(tmp_path: Path) -> None:
    root = tmp_path / "Documents"
    nested = root / ".git"
    root.mkdir()
    nested.mkdir()
    visible = root / "visible.bin"
    hidden = nested / "hidden.bin"
    visible.write_bytes(b"ok")
    hidden.write_bytes(b"no")

    records = tuple(
        scan_roots(
            (root,),
            ScanOptions(
                exact_file_identity=False,
                skip_directory_names=frozenset({"documents", ".git"}),
                root_skip_name_overrides=frozenset({str(root)}),
            ),
        )
    )
    paths = {Path(record.path) for record in records}
    assert visible in paths
    assert nested in paths
    assert hidden not in paths


def test_additional_path_below_pruned_profile_survives_outermost_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "home"
    project = profile / "Documents" / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    # Whole-profile traversal is no longer the packaged default, but remains a
    # supported explicit setting.  This test keeps covering its dedup boundary.
    rules = _rules_with_additional(project, include_user_profile=True)
    assert "documents" in rules.scan.skip_directory_names

    roots = app.scan_targets((), rules=rules)

    assert Path(profile.resolve()) in roots
    assert Path(project.resolve()) in roots


def test_audited_application_root_below_pruned_ancestor_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "home"
    audited = profile / "Documents" / "tool-cache"
    audited.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    defaults = default_rules()
    base = UserRules(
        scan=replace(defaults.scan, include_user_profile=True),
        delete=defaults.delete,
        keep=defaults.keep,
    )
    known = KnownCleanupRoot(
        path=audited,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Audited test cache",
        delete_root_itself=True,
        application_rule=_audited_rule(),
    )

    roots = app.scan_targets((known,), rules=base)

    assert Path(profile.resolve()) in roots
    assert Path(audited.resolve()) in roots


def test_user_exclusion_still_wins_over_specific_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "home"
    project = profile / "Documents" / "project"
    project.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    base = default_rules()
    rules = UserRules(
        scan=replace(
            base.scan,
            additional_paths=(str(project),),
            excluded_paths=(str(project),),
        ),
        delete=base.delete,
        keep=base.keep,
    )

    roots = app.scan_targets((), rules=rules)

    assert Path(project.resolve()) not in roots


def test_root_override_set_contains_only_selected_specific_roots(tmp_path: Path) -> None:
    explicit = tmp_path / "Documents"
    unrelated = tmp_path / "other"
    explicit.mkdir()
    unrelated.mkdir()
    rules = _rules_with_additional(explicit)

    overrides = app._scan_root_skip_name_overrides((explicit, unrelated), (), rules)

    assert normalise_path(explicit) in overrides
    assert normalise_path(unrelated) not in overrides
