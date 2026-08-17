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
from devclean.core.conda_cleanup import (
    conda_audited_tool_roots,
    conda_roots,
    evaluate_conda_path,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(
    tmp_path: Path,
) -> tuple[dict[str, str], Path, Path, Path, Path]:
    home = tmp_path / "home"
    root = home / "Miniconda3"
    cache = root / "pkgs"
    envs = root / "envs"
    state = home / ".conda"
    cache.mkdir(parents=True)
    envs.mkdir(parents=True)
    state.mkdir(parents=True)
    (home / ".condarc").write_text("channels:\n  - defaults\n", encoding="utf-8")
    env = {
        "USERPROFILE": str(home),
        "PROGRAMDATA": str(tmp_path / "ProgramData"),
        "TEMP": str(tmp_path / "Temp"),
        "DEVCLEAN_CONDA_ROOT_PREFIX": str(root),
        "CONDA_PKGS_DIRS": str(cache),
        "CONDA_ENVS_PATH": str(envs),
    }
    return env, root, cache, envs, state


def test_conda_effective_package_and_environment_roots_are_discovered(
    tmp_path: Path,
) -> None:
    env, root, cache, envs, state = _layout(tmp_path)

    roots = conda_roots(env)

    assert PureWindowsPath(str(root)) in roots.root_prefixes
    assert PureWindowsPath(str(cache)) in roots.package_cache_roots
    assert PureWindowsPath(str(envs)) in roots.environment_roots
    assert PureWindowsPath(str(state)) in roots.state_roots

    scan = application_scan_roots(env)
    assert PureWindowsPath(str(cache)) in scan
    assert PureWindowsPath(str(envs)) not in scan


def test_conda_cache_state_and_installation_are_protected_but_envs_are_user_owned(
    tmp_path: Path,
) -> None:
    env, root, cache, envs, state = _layout(tmp_path)
    cases = {
        cache / "numpy-2.0.0-py313_0" / "python313.dll": (
            "conda-package-cache-vendor-managed",
            DecisionOwner.KEEP,
        ),
        envs / "vision" / "python.exe": (
            "conda-environments",
            DecisionOwner.USER,
        ),
        root / "Library" / "bin" / "conda.dll": (
            "conda-root-prefix",
            DecisionOwner.KEEP,
        ),
        state / "environments.txt": (
            "conda-user-state",
            DecisionOwner.KEEP,
        ),
        Path(env["USERPROFILE"]) / ".condarc": (
            "conda-configuration",
            DecisionOwner.KEEP,
        ),
    }

    for path, (rule_id, owner) in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is owner


def test_conda_environment_metadata_is_protected_even_outside_known_envs(
    tmp_path: Path,
) -> None:
    env, _, _, _, _ = _layout(tmp_path)
    project_env_metadata = tmp_path / "project" / ".venv" / "conda-meta" / "history"

    rule = match_application_rule(project_env_metadata, env)

    assert rule is not None
    assert rule.rule_id == "conda-environment-metadata"
    assert rule.owner is DecisionOwner.KEEP


def test_conda_user_environment_is_projected_to_keep_in_generic_pipeline(
    tmp_path: Path,
) -> None:
    env, _, _, envs, _ = _layout(tmp_path)
    environment = envs / "vision"

    direct = evaluate_conda_path(
        environment,
        logical_size=8 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        environment=env,
    )
    generic = evaluate_application_path(
        environment,
        logical_size=8 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        environment=env,
    )

    assert direct is not None and direct.action is PolicyAction.USER_DECISION
    assert generic is not None and generic.action is PolicyAction.KEEP_PROTECTED


def test_conda_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, root, cache, envs, _ = _layout(tmp_path)

    assert conda_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert whole_tree_application_rule(envs, env) is None
    assert whole_tree_application_rule(root, env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_conda_package_cache_is_catalogued_report_only(tmp_path: Path) -> None:
    env, _, cache, _, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    cache_item = by_path[os.path.normcase(str(cache))]

    assert cache_item.category is CleanupCategory.CONDA_CACHE
    assert cache_item.policy is CleanupPolicy.REPORT_ONLY
    assert not cache_item.delete_root_itself
    assert cache_item.application_rule is None


def test_conda_process_dispatch_is_independent_from_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "conda_process_running", lambda: True)
    monkeypatch.setattr(application_cleanup, "uv_process_running", lambda: False)
    assert application_cleanup.application_process_running("conda")

    monkeypatch.setattr(application_cleanup, "conda_process_running", lambda: False)
    monkeypatch.setattr(application_cleanup, "uv_process_running", lambda: True)
    assert not application_cleanup.application_process_running("conda")
