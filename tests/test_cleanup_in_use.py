"""A running program blocks only what it really cannot lose while it runs.

Everything runs on folders inside ``tmp_path``; no program is started and the
running-program probe is a fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.application_cleanup as application_cleanup
from devclean.core.application_cleanup import RUNNING_OWNER_REFUSAL, process_guard_refusal


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
