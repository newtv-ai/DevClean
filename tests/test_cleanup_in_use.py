"""A running program blocks only what it really cannot lose while it runs.

Everything runs on folders inside ``tmp_path``; no program is started and the
running-program probe is a fake.
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pytest

import devclean.core.application_cleanup as application_cleanup
import devclean.core.chrome_cleanup as chrome_cleanup
import devclean.core.claude_cleanup as claude_cleanup
from devclean.core.application_cleanup import (
    RUNNING_OWNER_REFUSAL,
    ApplicationCleanupRule,
    DecisionOwner,
    PolicyAction,
    match_application_rule,
    must_close_before_cleanup,
    must_close_reason,
    process_guard_detail,
    process_guard_refusal,
)
from devclean.core.triage import ReviewLane, TriageItem, triage_file
from devclean.core.user_rules import default_rules
from devclean.scanner.filesystem import ScanRecord, ScanRecordKind

_NOW = datetime(2026, 11, 10, tzinfo=UTC)
_OLD_NS = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1_000_000_000)


def test_a_running_browser_no_longer_blocks_its_cache_but_a_session_marker_still_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    profile = local / "Google" / "Chrome" / "User Data" / "Default"
    caches = [
        profile / "Cache",
        profile / "Code Cache",
        local / "Google" / "GoogleUpdater" / "crx_cache",
    ]
    for folder in caches:
        folder.mkdir(parents=True)
    # A running Claude session may still use the working-directory marker it left.
    marker = home / "source" / "tmpclaude-a1b2-cwd"
    marker.parent.mkdir(parents=True)
    marker.write_text("C:\\work")
    running = {"value": True}
    monkeypatch.setattr(
        application_cleanup, "application_process_running", lambda _app: running["value"]
    )

    # Deleting goes ahead; files the browser holds open are skipped one by one.
    assert [process_guard_refusal(folder) for folder in caches] == [None, None, None]
    # What a running program really cannot lose still waits for it to close.
    assert process_guard_refusal(marker) == RUNNING_OWNER_REFUSAL
    running["value"] = False
    assert process_guard_refusal(marker) is None
    # IDE index caches stay "close it first": the IDE keeps maintaining them.
    tolerated = application_cleanup.cleanable_while_running()
    assert {"chrome-http-cache", "chrome-updater-crx-cache"} <= tolerated
    assert "jetbrains-index-cache" not in tolerated
    assert "claude-cwd-temp-marker" not in tolerated


def test_the_scan_lists_what_the_pre_delete_guard_lets_go_while_programs_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    # Both sides see the same running programs: the scan's evaluators ask their
    # own module's probe, the pre-delete guard asks per application.  (Cached
    # like the real probes, which the guard clears before asking again.)
    monkeypatch.setattr(chrome_cleanup, "chrome_process_running", cache(lambda: True))
    monkeypatch.setattr(claude_cleanup, "claude_process_running", cache(lambda: True))
    monkeypatch.setattr(application_cleanup, "application_process_running", lambda _app: True)
    rules = default_rules()
    profile = local / "Google" / "Chrome" / "User Data" / "Default"
    cache_files = [
        profile / "Cache" / "Cache_Data" / "f_000001",
        profile / "Code Cache" / "js" / "index",
        local / "Google" / "GoogleUpdater" / "crx_cache" / "payload.crx3",
    ]
    marker = home / "source" / "tmpclaude-a1b2-cwd"

    def scanned(path: Path) -> TriageItem:
        return triage_file(
            ScanRecord(
                root=str(home),
                path=str(path),
                kind=ScanRecordKind.FILE,
                depth=len(path.relative_to(home).parts),
                logical_size=64 * 1024 * 1024,
                last_write_time_ns=_OLD_NS,
            ),
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
            now=_NOW,
        )

    for path in cache_files:
        rule = match_application_rule(path)
        assert rule is not None and rule.requires_process_closed, path
        assert not must_close_before_cleanup(rule)
        item = scanned(path)
        assert item.lane is ReviewLane.DETERMINISTIC_CANDIDATE, (path, item.reason)
        assert "已关闭" not in item.reason and "requires_process_closed" not in item.tags
        assert process_guard_refusal(path) is None

    rule = match_application_rule(marker)
    assert rule is not None and must_close_before_cleanup(rule)
    item = scanned(marker)
    assert item.lane is ReviewLane.REPORT_ONLY
    assert "应用仍在运行" in item.reason
    assert process_guard_refusal(marker) == RUNNING_OWNER_REFUSAL


def _all_audited_rules() -> dict[str, ApplicationCleanupRule]:
    """Every application rule the audited modules define, by rule id."""

    import devclean.core as core

    found: dict[str, ApplicationCleanupRule] = {}

    def walk(value: object) -> None:
        if isinstance(value, ApplicationCleanupRule):
            found.setdefault(value.rule_id, value)
        elif isinstance(value, (tuple, list, set, frozenset)):
            for member in value:
                walk(member)
        elif isinstance(value, dict):
            for member in value.values():
                walk(member)

    for module_info in pkgutil.iter_modules(core.__path__):
        module = importlib.import_module(f"devclean.core.{module_info.name}")
        for value in vars(module).values():
            walk(value)
    return found


def test_every_rule_that_asks_for_its_program_closed_is_decided_with_a_reason() -> None:
    # User decision 2026-11-11: a running program does not block its data as a
    # whole.  Each rule that asks for its program to be closed is either cleaned
    # while it runs, or waits for a reason that is written down and shown.
    rules = _all_audited_rules()
    # Protected (KEEP) data is never deleted, running program or not.
    asking = {
        rule_id
        for rule_id, rule in rules.items()
        if rule.requires_process_closed and rule.owner is not DecisionOwner.KEEP
    }
    tolerated = application_cleanup.cleanable_while_running()
    reasons = application_cleanup._MUST_CLOSE_REASONS

    assert asking, "the audited rules were not found"
    assert not set(reasons) & tolerated
    assert asking <= tolerated | set(reasons), sorted(asking - tolerated - set(reasons))
    assert set(reasons) <= asking, sorted(set(reasons) - asking)
    assert all(reason.strip() for reason in reasons.values())
    for rule_id in asking:
        rule = rules[rule_id]
        expected = reasons.get(rule_id)
        assert must_close_reason(rule) == expected, rule_id


def test_what_the_user_saw_refused_is_cleaned_while_chrome_and_codex_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The c20d81a run on Windows: 14 rows refused with "所属程序正在运行".
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    chrome = local / "Google" / "Chrome" / "User Data"
    codex_web = (
        local / "Packages" / "OpenAI.Codex_2p2nqsd0c76g0" / "LocalCache" / "Roaming"
        / "Codex" / "web" / "Codex"
    )
    seen = {
        chrome / "Default" / "Cache": "chrome-http-cache",
        chrome / "GPUPersistentCache": "chrome-gpu-persistent-cache",
        codex_web / "Cache": "codex-desktop-browser-cache",
        codex_web / "Code Cache": "codex-desktop-browser-cache",
        codex_web / "GPUCache": "codex-desktop-browser-cache",
        codex_web / "Default" / "Cache": "codex-desktop-default-browser-cache",
        home / ".codex" / ".tmp" / "plugins": "codex-plugin-marketplace-snapshot",
    }
    for folder in seen:
        folder.mkdir(parents=True)
    monkeypatch.setattr(application_cleanup, "application_process_running", lambda _app: True)

    for folder, rule_id in seen.items():
        rule = match_application_rule(folder)
        assert rule is not None and rule.rule_id == rule_id, folder
        assert process_guard_refusal(folder) is None, folder
        decision = application_cleanup.evaluate_application_path(
            folder / "entry",
            logical_size=64 * 1024 * 1024,
            last_used=_NOW.replace(year=2025),
            now=_NOW,
            process_running=True,
        )
        assert decision is not None
        assert decision.action is not PolicyAction.TOOL_KEEP_IN_USE, folder


def test_a_refusal_names_the_program_and_why_it_has_to_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from devclean.core.postscan_cleanup import CleanupRefusal
    from devclean.ui import app

    home = tmp_path / "home"
    monkeypatch.setenv("USERPROFILE", str(home))
    marker = home / "source" / "tmpclaude-a1b2-cwd"
    marker.parent.mkdir(parents=True)
    marker.write_text("C:\\work")
    monkeypatch.setattr(application_cleanup, "application_process_running", lambda _app: True)

    detail = process_guard_detail(marker)
    assert detail == f"{RUNNING_OWNER_REFUSAL}: claude: claude-cwd-temp-marker"
    # The plain refusal is unchanged for every other caller.
    assert process_guard_refusal(marker) == RUNNING_OWNER_REFUSAL
    shown = app._reason_of(CleanupRefusal(detail))
    assert shown.startswith("Claude Code 正在运行，这一项要等它关闭后再清理：")  # noqa: RUF001
    assert shown.endswith(application_cleanup._MUST_CLOSE_REASONS["claude-cwd-temp-marker"])
