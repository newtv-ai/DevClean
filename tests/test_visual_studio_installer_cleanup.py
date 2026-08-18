from __future__ import annotations

import os
import winreg
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.application_cleanup as application_cleanup
import devclean.core.visual_studio_installer_cleanup as visual_studio_installer_cleanup
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
from devclean.core.user_rules import default_rules
from devclean.core.visual_studio_installer_cleanup import (
    visual_studio_installer_audited_tool_roots,
    visual_studio_installer_roots,
)
from devclean.platform.windows.registry import RegistryStringLookup, RegistryStringValue

_NOW = datetime(2026, 8, 18, tzinfo=UTC)
_POLICY_KEY = r"SOFTWARE\Policies\Microsoft\VisualStudio\Setup"


@pytest.fixture(autouse=True)
def _no_cache_path_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: RegistryStringLookup(None, True),
    )


def _layout(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    program_data = tmp_path / "ProgramData"
    cache = program_data / "Microsoft" / "VisualStudio" / "Packages"
    metadata = cache / "_Instances"
    metadata.mkdir(parents=True)
    env = {"PROGRAMDATA": str(program_data)}
    return env, cache, metadata


def _policy(value: str, value_type: int = winreg.REG_SZ) -> RegistryStringLookup:
    return RegistryStringLookup(
        RegistryStringValue(value, value_type, _POLICY_KEY),
        True,
    )


def test_visual_studio_installer_default_cache_is_discovered(tmp_path: Path) -> None:
    env, cache, metadata = _layout(tmp_path)

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == (PureWindowsPath(str(cache)),)
    assert roots.instance_metadata_roots == (PureWindowsPath(str(metadata)),)
    assert PureWindowsPath(str(cache)) in application_scan_roots(env)


def test_visual_studio_installer_registry_cachepath_replaces_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, default_cache, _ = _layout(tmp_path)
    redirected = tmp_path / "VSCache"
    redirected.mkdir()
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: _policy(str(redirected)),
    )

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == (PureWindowsPath(str(redirected)),)
    assert PureWindowsPath(str(default_cache)) not in application_scan_roots(env)
    assert PureWindowsPath(str(redirected)) in application_scan_roots(env)


def test_visual_studio_installer_expandable_cachepath_uses_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _ = _layout(tmp_path)
    redirected = Path(env["PROGRAMDATA"]) / "VS-Payloads"
    redirected.mkdir()
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: _policy(
            r"%PROGRAMDATA%\VS-Payloads",
            winreg.REG_EXPAND_SZ,
        ),
    )

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == (PureWindowsPath(str(redirected)),)


def test_visual_studio_installer_unknown_expandable_variable_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _ = _layout(tmp_path)
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: _policy(r"%UNKNOWN_VS_CACHE%\Packages", winreg.REG_EXPAND_SZ),
    )

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == ()
    assert roots.instance_metadata_roots == ()


def test_visual_studio_installer_inconclusive_registry_lookup_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _ = _layout(tmp_path)
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: RegistryStringLookup(None, False),
    )

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == ()
    assert roots.instance_metadata_roots == ()


def test_visual_studio_installer_relative_registry_cachepath_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _, _ = _layout(tmp_path)
    monkeypatch.setattr(
        visual_studio_installer_cleanup,
        "first_hklm_string_value",
        lambda *_args: _policy(r"relative\Packages"),
    )

    roots = visual_studio_installer_roots(env)

    assert roots.package_cache_roots == ()
    assert roots.instance_metadata_roots == ()


def test_visual_studio_installer_missing_programdata_fails_closed() -> None:
    roots = visual_studio_installer_roots({})

    assert roots.package_cache_roots == ()
    assert roots.instance_metadata_roots == ()


def test_visual_studio_installer_payload_cache_stays_keep_when_large_and_old(
    tmp_path: Path,
) -> None:
    env, cache, _ = _layout(tmp_path)
    payload = cache / "Microsoft.VisualStudio.Component.VC.Tools.x86.x64" / "payload.vsix"

    rule = match_application_rule(payload, env)
    assert rule is not None
    assert rule.rule_id == "visual-studio-installer-package-cache-mixed"
    assert rule.owner is DecisionOwner.KEEP

    decision = evaluate_application_path(
        cache,
        logical_size=80 * 1024**3,
        last_used=_NOW - timedelta(days=730),
        now=_NOW,
        process_running=False,
        environment=env,
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_visual_studio_installer_instance_metadata_has_specific_keep_rule(
    tmp_path: Path,
) -> None:
    env, _, metadata = _layout(tmp_path)

    rule = match_application_rule(metadata / "abc123" / "state.json", env)

    assert rule is not None
    assert rule.rule_id == "visual-studio-installer-instance-metadata"
    assert rule.owner is DecisionOwner.KEEP


def test_visual_studio_installer_never_grants_raw_whole_tree_authority(
    tmp_path: Path,
) -> None:
    env, cache, _ = _layout(tmp_path)

    assert visual_studio_installer_audited_tool_roots(env) == ()
    assert whole_tree_application_rule(cache, env) is None
    assert not application_cleanup.process_guard_allows(cache, env)


def test_visual_studio_installer_cache_is_catalogued_report_only(
    tmp_path: Path,
) -> None:
    env, cache, _ = _layout(tmp_path)

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(item.path)): item for item in discovered}
    item = by_path[os.path.normcase(str(cache))]

    assert item.category is CleanupCategory.INSTALLERS_DOWNLOADS
    assert item.policy is CleanupPolicy.REPORT_ONLY
    assert not item.delete_root_itself
    assert item.application_rule is None


def test_visual_studio_installer_does_not_claim_project_build_outputs(
    tmp_path: Path,
) -> None:
    env, _, _ = _layout(tmp_path)
    project = tmp_path / "src" / "sample"

    assert match_application_rule(project / "bin" / "Debug" / "app.exe", env) is None
    assert match_application_rule(project / "obj" / "Debug" / "app.obj", env) is None


def test_visual_studio_installer_process_dispatch_does_not_alias_electron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        application_cleanup, "visual_studio_installer_process_running", lambda: True
    )
    monkeypatch.setattr(application_cleanup, "electron_process_running", lambda: False)
    assert application_cleanup.application_process_running("visual_studio_installer")

    monkeypatch.setattr(
        application_cleanup, "visual_studio_installer_process_running", lambda: False
    )
    monkeypatch.setattr(application_cleanup, "electron_process_running", lambda: True)
    assert not application_cleanup.application_process_running("visual_studio_installer")
