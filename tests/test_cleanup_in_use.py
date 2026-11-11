"""A running program blocks only what it really cannot lose while it runs.

Everything runs on folders inside ``tmp_path``; no program is started and the
running-program probe is a fake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import cache
from pathlib import Path

import pytest

import devclean.core.application_cleanup as application_cleanup
import devclean.core.chrome_cleanup as chrome_cleanup
import devclean.core.claude_cleanup as claude_cleanup
from devclean.core.application_cleanup import (
    RUNNING_OWNER_REFUSAL,
    match_application_rule,
    must_close_before_cleanup,
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
