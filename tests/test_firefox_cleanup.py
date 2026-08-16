from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath

from devclean.core._application_cleanup_impl import DecisionOwner, PolicyAction
from devclean.core.application_cleanup import (
    application_scan_roots,
    evaluate_application_path,
    match_application_rule,
    process_guard_allows,
    whole_tree_application_rule,
)
from devclean.core.cleanup_catalog import (
    CleanupCategory,
    CleanupPolicy,
    discover_known_cleanup_roots,
)
from devclean.core.firefox_cleanup import (
    _profile_switch_path,
    evaluate_firefox_path,
    firefox_roots,
    match_firefox_rule,
    whole_tree_firefox_rule,
)
from devclean.core.user_rules import default_rules

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_MIB = 1024**2


def _env() -> dict[str, str]:
    return {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    }


def test_firefox_roaming_root_protects_profile_registry_and_cross_profile_state() -> None:
    protected = (
        r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\profiles.ini",
        r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\installs.ini",
        (
            r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profile Groups"
            r"\profile-group.sqlite"
        ),
    )
    for path in protected:
        rule = match_application_rule(path, _env())
        assert rule is not None
        assert rule.rule_id == "firefox-roaming-state"
        assert rule.owner is DecisionOwner.KEEP
        assert not process_guard_allows(path, _env())


def test_firefox_default_persistent_profile_is_keep_but_exact_cache_children_are_tool() -> None:
    profile = r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles\abc.default-release"
    history = match_application_rule(profile + r"\places.sqlite", _env())
    cache = match_application_rule(profile + r"\cache2\entries\abcdef", _env())
    startup = match_application_rule(profile + r"\startupCache\startupCache.8.little", _env())

    assert history is not None
    assert history.rule_id == "firefox-persistent-profile-state"
    assert history.owner is DecisionOwner.KEEP
    assert cache is not None
    assert cache.rule_id == "firefox-cache2"
    assert cache.owner is DecisionOwner.TOOL
    assert startup is not None
    assert startup.rule_id == "firefox-startup-cache"
    assert startup.owner is DecisionOwner.TOOL

    decision = evaluate_application_path(
        profile + r"\places.sqlite",
        logical_size=64 * _MIB,
        last_used=_NOW - timedelta(days=180),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.action is PolicyAction.KEEP_PROTECTED


def test_firefox_local_profile_is_cache_only_tool_root() -> None:
    profile = r"C:\Users\alice\AppData\Local\Mozilla\Firefox\Profiles\abc.default-release"
    rule = match_firefox_rule(profile + r"\cache2\entries\abcdef", _env())
    assert rule is not None
    assert rule.owner is DecisionOwner.TOOL
    assert rule.rule_id == "firefox-cache2"

    decision = evaluate_firefox_path(
        profile,
        logical_size=128 * _MIB,
        last_used=_NOW - timedelta(days=45),
        now=_NOW,
        process_running=False,
        environment=_env(),
    )
    assert decision is not None
    assert decision.rule.rule_id == "firefox-local-profile-cache-root"
    assert decision.action is PolicyAction.TOOL_DELETE


def test_explicit_local_profile_does_not_authorize_sibling_directories(tmp_path: Path) -> None:
    parent = tmp_path / "shared-cache-parent"
    profile = parent / "FirefoxLocalProfile"
    sibling = parent / "UnrelatedApp"
    profile.mkdir(parents=True)
    sibling.mkdir()
    env = {
        **_env(),
        "DEVCLEAN_FIREFOX_LOCAL_PROFILE_DIR": str(profile),
    }

    roots = firefox_roots(env)
    assert PureWindowsPath(str(profile)) in roots.local_profiles
    assert PureWindowsPath(str(parent)) not in roots.local_parents

    profile_rule = match_application_rule(profile / "cache2" / "entry", env)
    sibling_rule = match_application_rule(sibling / "cache2" / "entry", env)
    assert profile_rule is not None
    assert profile_rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(profile, env) is not None
    assert sibling_rule is None
    assert whole_tree_application_rule(sibling, env) is None


def test_custom_profile_never_gains_whole_tree_authority_but_cache_child_can(tmp_path: Path) -> None:
    profile = tmp_path / "portable-profile"
    cache = profile / "cache2"
    cache.mkdir(parents=True)
    (profile / "places.sqlite").write_text("history", encoding="utf-8")
    env = {
        **_env(),
        "DEVCLEAN_FIREFOX_PROFILE_DIR": str(profile),
    }

    profile_rule = match_application_rule(profile / "places.sqlite", env)
    cache_rule = match_application_rule(cache / "entry", env)
    assert profile_rule is not None
    assert profile_rule.rule_id == "firefox-persistent-profile-state"
    assert profile_rule.owner is DecisionOwner.KEEP
    assert cache_rule is not None
    assert cache_rule.rule_id == "firefox-cache2"
    assert cache_rule.owner is DecisionOwner.TOOL
    assert whole_tree_application_rule(profile, env) is None
    whole_cache = whole_tree_application_rule(cache, env)
    assert whole_cache is not None
    assert whole_cache.rule_id == "firefox-cache2"


def test_firefox_profile_switch_parser_supports_single_and_double_dash() -> None:
    cases = {
        r'"C:\Program Files\Mozilla Firefox\firefox.exe" --profile "D:\Firefox Profiles\One"': (
            r"D:\Firefox Profiles\One"
        ),
        r'firefox.exe -profile D:\PortableFirefox\Profile': r"D:\PortableFirefox\Profile",
        r'firefox.exe --profile=D:\Profiles\Test': r"D:\Profiles\Test",
    }
    for command_line, expected in cases.items():
        assert _profile_switch_path(command_line) == expected
    assert _profile_switch_path(r"firefox.exe -P work") is None


def test_firefox_update_logs_are_exactly_scoped_under_updates_directory() -> None:
    update = r"C:\ProgramData\Mozilla\updates\install-hash"
    current_log = match_application_rule(update + r"\updates\0\update.log", _env())
    last_log = match_application_rule(update + r"\updates\last-update.log", _env())
    misplaced_log = match_application_rule(update + r"\last-update.log", _env())
    payload = match_application_rule(update + r"\updates\0\update.mar", _env())

    assert current_log is not None and current_log.owner is DecisionOwner.TOOL
    assert last_log is not None and last_log.owner is DecisionOwner.TOOL
    assert misplaced_log is not None
    assert misplaced_log.rule_id == "firefox-update-state"
    assert misplaced_log.owner is DecisionOwner.KEEP
    assert payload is not None
    assert payload.rule_id == "firefox-update-state"
    assert payload.owner is DecisionOwner.KEEP


def test_default_firefox_roots_include_roaming_state_and_local_profiles() -> None:
    roots = firefox_roots(_env())
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox"
    ) in roots.state_roots
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Roaming\Mozilla\Firefox\Profiles"
    ) in roots.persistent_parents
    assert PureWindowsPath(
        r"C:\Users\alice\AppData\Local\Mozilla\Firefox\Profiles"
    ) in roots.local_parents


def test_firefox_facade_catalogues_only_audited_tool_roots_for_whole_tree(
    tmp_path: Path,
) -> None:
    roaming = tmp_path / "Roaming" / "Mozilla" / "Firefox"
    persistent = roaming / "Profiles" / "abc.default-release"
    local_profile = (
        tmp_path / "Local" / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release"
    )
    persistent.mkdir(parents=True)
    local_profile.mkdir(parents=True)
    (persistent / "places.sqlite").write_text("history", encoding="utf-8")
    (local_profile / "cache2").mkdir()
    env = {
        "USERPROFILE": str(tmp_path / "home"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "PROGRAMDATA": str(tmp_path / "ProgramData"),
        "TEMP": str(tmp_path / "Temp"),
    }

    scan_roots = set(application_scan_roots(env))
    assert PureWindowsPath(str(roaming)) in scan_roots
    assert PureWindowsPath(str(local_profile.parent)) in scan_roots
    assert whole_tree_application_rule(roaming, env) is None
    local_whole = whole_tree_application_rule(local_profile, env)
    assert local_whole is not None
    assert local_whole.rule_id == "firefox-local-profile-cache-root"

    discovered = discover_known_cleanup_roots(default_rules().scan, env)
    by_path = {os.path.normcase(str(root.path)): root for root in discovered}
    roaming_root = by_path[os.path.normcase(str(roaming))]
    local_root = by_path[os.path.normcase(str(local_profile))]
    assert roaming_root.policy is CleanupPolicy.REPORT_ONLY
    assert not roaming_root.delete_root_itself
    assert local_root.category is CleanupCategory.BROWSER_CACHE
    assert local_root.policy is CleanupPolicy.VENDOR_MANAGED
    assert local_root.delete_root_itself
