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
from devclean.core.ollama_cleanup import ollama_audited_tool_roots, ollama_roots
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    ollama_home = home / ".ollama"
    models = ollama_home / "models"
    models.mkdir(parents=True)
    server_config = ollama_home / "server.json"
    server_config.write_text('{"origins": []}', encoding="utf-8")
    env = {"USERPROFILE": str(home)}
    return env, ollama_home, models, server_config


def test_ollama_default_windows_model_root_is_discovered(tmp_path: Path) -> None:
    env, ollama_home, models, server_config = _layout(tmp_path)

    roots = ollama_roots(env)

    assert roots.home_roots == (PureWindowsPath(str(ollama_home)),)
    assert roots.model_roots == (PureWindowsPath(str(models)),)
    assert roots.configuration_paths == (PureWindowsPath(str(server_config)),)
    assert PureWindowsPath(str(models)) in application_scan_roots(env)


def test_ollama_models_override_replaces_default_root(tmp_path: Path) -> None:
    env, _, default_models, _ = _layout(tmp_path)
    custom = tmp_path / "model-drive" / "ollama"
    custom.mkdir(parents=True)
    env["OLLAMA_MODELS"] = str(custom)

    roots = ollama_roots(env)

    assert roots.model_roots == (PureWindowsPath(str(custom)),)
    assert PureWindowsPath(str(default_models)) not in roots.model_roots


def test_ollama_model_store_is_user_owned_not_generic_tool(tmp_path: Path) -> None:
    env, _, models, _ = _layout(tmp_path)
    blob = models / "blobs" / "sha256-abcdef"

    rule = match_application_rule(blob, env)

    assert rule is not None
    assert rule.rule_id == "ollama-model-store"
    assert rule.owner is DecisionOwner.USER

    decision = evaluate_application_path(
        models,
        logical_size=100 * 1024**3,
        last_used=_NOW - timedelta(days=365),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_ollama_home_configuration_and_unknown_state_are_keep(tmp_path: Path) -> None:
    env, ollama_home, _, server_config = _layout(tmp_path)
    cases = {
        server_config: "ollama-server-configuration",
        ollama_home / "id_ed25519": "ollama-home-state",
        ollama_home / "future-state" / "state.bin": "ollama-home-state",
    }

    for path, rule_id in cases.items():
        rule = match_application_rule(path, env)
        assert rule is not None
        assert rule.rule_id == rule_id
        assert rule.owner is DecisionOwner.KEEP


def test_ollama_never_grants_raw_whole_tree_authority(tmp_path: Path) -> None:
    env, _, models, _ = _layout(tmp_path)

    assert ollama_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(models, env) is None
    assert not application_cleanup.process_guard_allows(models, env)


def test_ollama_model_root_is_catalogued_report_only(tmp_path: Path) -> None:
    env, _, models, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(models))]

    assert item.category is CleanupCategory.OLLAMA_MODELS
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_ollama_process_dispatch_is_independent_from_huggingface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(application_cleanup, "ollama_process_running", lambda: True)
    monkeypatch.setattr(
        application_cleanup,
        "huggingface_process_running",
        lambda: False,
    )
    assert application_cleanup.application_process_running("ollama")

    monkeypatch.setattr(application_cleanup, "ollama_process_running", lambda: False)
    monkeypatch.setattr(
        application_cleanup,
        "huggingface_process_running",
        lambda: True,
    )
    assert not application_cleanup.application_process_running("ollama")
