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
from devclean.core.maven_cleanup import maven_audited_tool_roots, maven_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    m2 = home / ".m2"
    repo = m2 / "repository"
    repo.mkdir(parents=True)
    settings = m2 / "settings.xml"
    settings.write_text(
        "<settings><servers><server><id>x</id><password>secret</password>"
        "</server></servers></settings>",
        encoding="utf-8",
    )
    toolchains = m2 / "toolchains.xml"
    toolchains.write_text("<toolchains />", encoding="utf-8")
    env = {"USERPROFILE": str(home)}
    return env, repo, settings, toolchains


def test_maven_default_windows_local_repository_is_discovered(tmp_path: Path) -> None:
    env, repo, settings, toolchains = _layout(tmp_path)

    roots = maven_roots(env)

    assert roots.local_repository_roots == (PureWindowsPath(str(repo)),)
    assert PureWindowsPath(str(settings)) in roots.config_paths
    assert PureWindowsPath(str(toolchains)) in roots.config_paths
    assert PureWindowsPath(str(repo)) in application_scan_roots(env)


def test_maven_settings_local_repository_override_is_discovered(tmp_path: Path) -> None:
    env, default_repo, settings, _ = _layout(tmp_path)
    custom = tmp_path / "cache-drive" / "maven-repository"
    custom.mkdir(parents=True)
    settings.write_text(
        "<settings><localRepository>${user.home}/../cache-drive/maven-repository"
        "</localRepository></settings>",
        encoding="utf-8",
    )

    roots = maven_roots(env)

    expected = PureWindowsPath(str(Path(env["USERPROFILE"]) / ".." / "cache-drive" / "maven-repository"))
    assert roots.local_repository_roots == (expected,)
    assert PureWindowsPath(str(default_repo)) not in roots.local_repository_roots


def test_maven_explicit_repo_property_takes_precedence(tmp_path: Path) -> None:
    env, _, settings, _ = _layout(tmp_path)
    from_settings = tmp_path / "from-settings"
    from_args = tmp_path / "from-args"
    settings.write_text(
        f"<settings><localRepository>{from_settings}</localRepository></settings>",
        encoding="utf-8",
    )
    env["MAVEN_ARGS"] = f'-B -Dmaven.repo.local="{from_args}" verify'

    roots = maven_roots(env)

    assert roots.local_repository_roots == (PureWindowsPath(str(from_args)),)


def test_maven_repository_is_mixed_keep_even_for_remote_looking_artifact(
    tmp_path: Path,
) -> None:
    env, repo, _, _ = _layout(tmp_path)
    artifact = repo / "org" / "apache" / "commons" / "commons-lang3" / "3.17.0" / "a.jar"

    rule = match_application_rule(artifact, env)

    assert rule is not None
    assert rule.rule_id == "maven-local-repository-mixed"
    assert rule.owner is DecisionOwner.KEEP


def test_maven_user_config_and_project_metadata_are_protected(tmp_path: Path) -> None:
    env, _, settings, toolchains = _layout(tmp_path)
    m2 = Path(env["USERPROFILE"]) / ".m2"
    security = m2 / "settings-security.xml"
    unknown = m2 / "future-state" / "state.bin"
    project = tmp_path / "work" / "app"

    cases = {
        settings: "maven-settings",
        toolchains: "maven-toolchains",
        security: "maven-security-settings",
        unknown: "maven-user-config-root",
        project / "pom.xml": "maven-project-metadata",
        project / ".mvn" / "maven.config": "maven-project-configuration",
        project / ".mvn" / "wrapper" / "maven-wrapper.properties": "maven-project-configuration",
    }
    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP


def test_maven_generic_pipeline_keeps_old_large_repository(tmp_path: Path) -> None:
    env, repo, _, _ = _layout(tmp_path)

    decision = evaluate_application_path(
        repo,
        logical_size=32 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )

    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_maven_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, repo, _, _ = _layout(tmp_path)

    assert maven_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(repo, env) is None
    assert not application_cleanup.process_guard_allows(repo, env)


def test_maven_repository_is_catalogued_report_only(tmp_path: Path) -> None:
    env, repo, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(repo))]

    assert item.category is CleanupCategory.MAVEN_REPOSITORY
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_maven_process_dispatch_is_independent_from_cargo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "maven_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "cargo_process_running", lambda: False)
    assert application_cleanup.application_process_running("maven")

    monkeypatch.setattr(application_cleanup, "maven_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "cargo_process_running", lambda: True)
    assert not application_cleanup.application_process_running("maven")
