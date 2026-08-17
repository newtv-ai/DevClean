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
from devclean.core.cargo_cleanup import cargo_audited_tool_roots, cargo_roots
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    home = tmp_path / "home"
    cargo_home = home / ".cargo"
    registry = cargo_home / "registry"
    git = cargo_home / "git"
    bin_root = cargo_home / "bin"
    credentials = cargo_home / "credentials.toml"
    for root in (registry, git, bin_root):
        root.mkdir(parents=True)
    credentials.write_text("[registry]\ntoken = 'secret'\n", encoding="utf-8")
    (cargo_home / "config.toml").write_text("[net]\noffline = false\n", encoding="utf-8")
    (cargo_home / ".crates2.json").write_text("{}", encoding="utf-8")
    env = {"USERPROFILE": str(home)}
    return env, registry, git, bin_root, credentials


def test_cargo_default_home_and_cache_roots_are_discovered(tmp_path: Path) -> None:
    env, registry, git, bin_root, credentials = _layout(tmp_path)

    roots = cargo_roots(env)

    assert roots.home_roots == (PureWindowsPath(str(Path(env["USERPROFILE"]) / ".cargo")),)
    assert roots.registry_roots == (PureWindowsPath(str(registry)),)
    assert roots.git_roots == (PureWindowsPath(str(git)),)
    assert roots.bin_roots == (PureWindowsPath(str(bin_root)),)
    assert PureWindowsPath(str(credentials)) in roots.credential_paths

    scan = application_scan_roots(env)
    assert PureWindowsPath(str(registry)) in scan
    assert PureWindowsPath(str(git)) in scan
    assert PureWindowsPath(str(bin_root)) not in scan


def test_cargo_home_override_replaces_default_root(tmp_path: Path) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    custom = tmp_path / "cargo-state"
    env["CARGO_HOME"] = str(custom)

    roots = cargo_roots(env)

    assert roots.home_roots == (PureWindowsPath(str(custom)),)
    assert roots.registry_roots == (PureWindowsPath(str(custom / "registry")),)
    assert roots.git_roots == (PureWindowsPath(str(custom / "git")),)


def test_cargo_cache_and_persistent_state_are_keep(tmp_path: Path) -> None:
    env, registry, git, bin_root, credentials = _layout(tmp_path)
    cargo_home = Path(env["USERPROFILE"]) / ".cargo"
    cases = {
        registry / "cache" / "index.crates.io" / "serde.crate": (
            "cargo-registry-cache-vendor-managed",
            RebuildCost.HIGH,
        ),
        git / "db" / "repo.git" / "HEAD": (
            "cargo-git-cache-vendor-managed",
            RebuildCost.HIGH,
        ),
        bin_root / "cargo-edit.exe": ("cargo-installed-binaries", RebuildCost.HIGH),
        credentials: ("cargo-credentials", RebuildCost.HIGH),
        cargo_home / "config.toml": ("cargo-configuration", RebuildCost.HIGH),
        cargo_home / ".crates2.json": ("cargo-install-metadata", RebuildCost.HIGH),
        cargo_home / "future-state" / "data.bin": ("cargo-home-state", RebuildCost.HIGH),
    }

    for path, (rule_id, rebuild_cost) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP
        assert rule.rebuild_cost is rebuild_cost


def test_cargo_project_metadata_and_project_config_are_protected(tmp_path: Path) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    project = tmp_path / "work" / "crate"

    for name in ("Cargo.toml", "Cargo.lock"):
        rule = match_application_rule(project / name, env)
        assert rule is not None
        assert rule.rule_id == "cargo-project-metadata"
        assert rule.owner is DecisionOwner.KEEP

    rule = match_application_rule(project / ".cargo" / "config.toml", env)
    assert rule is not None
    assert rule.rule_id == "cargo-project-configuration"
    assert rule.owner is DecisionOwner.KEEP


def test_cargo_project_target_directory_is_not_claimed_by_global_cache_profile(
    tmp_path: Path,
) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    target = tmp_path / "work" / "crate" / "target" / "debug" / "app.exe"

    assert match_application_rule(target, env) is None


def test_cargo_generic_pipeline_keeps_old_large_global_cache(tmp_path: Path) -> None:
    env, registry, git, _, _ = _layout(tmp_path)

    for root in (registry, git):
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


def test_cargo_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, registry, git, _, _ = _layout(tmp_path)

    assert cargo_audited_tool_roots(env) == ()
    for root in (registry, git):
        assert whole_tree_application_rule(root, env) is None
        assert not application_cleanup.process_guard_allows(root, env)


def test_cargo_cache_roots_are_catalogued_report_only(tmp_path: Path) -> None:
    env, registry, git, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}

    for root in (registry, git):
        item = by_path[os.path.normcase(str(root))]
        assert item.category is CleanupCategory.CARGO_REGISTRY
        assert item.policy is CleanupPolicy.REPORT_ONLY
        assert not item.delete_root_itself
        assert item.application_rule is None


def test_cargo_process_dispatch_is_independent_from_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "cargo_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "go_process_running", lambda: False)
    assert application_cleanup.application_process_running("cargo")

    monkeypatch.setattr(application_cleanup, "cargo_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "go_process_running", lambda: True)
    assert not application_cleanup.application_process_running("cargo")
