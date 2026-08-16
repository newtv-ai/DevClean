from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from devclean.core.codex_history import (
    CodexHistoryError,
    delete_codex_threads,
    prune_codex_input_history,
    scan_codex_sessions,
    select_codex_sessions_older_than,
    summarize_codex_input_history,
    summarize_codex_sessions,
)

_NOW = datetime(2026, 8, 16, tzinfo=UTC)
_UUID_OLD = "11111111-1111-4111-8111-111111111111"
_UUID_NEW = "22222222-2222-4222-8222-222222222222"


def _rollout(home: Path, thread_id: str, *, days_old: int, archived: bool = False) -> Path:
    root = home / ("archived_sessions" if archived else "sessions") / "2026" / "01" / "01"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-2026-01-01T00-00-00-{thread_id}.jsonl"
    path.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    modified = _NOW - timedelta(days=days_old)
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path


def test_session_inventory_uses_rollout_mtime_and_age_buckets(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    old = _rollout(home, _UUID_OLD, days_old=120)
    new = _rollout(home, _UUID_NEW, days_old=10, archived=True)

    sessions = scan_codex_sessions(home)
    summaries = summarize_codex_sessions(sessions, now=_NOW)

    assert [item.path for item in sessions] == [old, new]
    assert sessions[0].thread_id == _UUID_OLD
    assert sessions[1].archived
    assert [(item.cutoff_days, item.count) for item in summaries] == [
        (30, 1),
        (90, 1),
        (180, 0),
    ]
    assert select_codex_sessions_older_than(sessions, 90, now=_NOW) == (sessions[0],)


def test_input_history_summary_and_prune_preserve_recent_and_unknown_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = tmp_path / "history.jsonl"
    old_ts = (_NOW - timedelta(days=120)).timestamp()
    recent_ts = (_NOW - timedelta(days=5)).timestamp()
    old_line = json.dumps({"session_id": "old", "ts": old_ts, "text": "old"}) + "\n"
    recent_line = json.dumps({"session_id": "new", "ts": recent_ts, "text": "new"}) + "\n"
    malformed = "not-json-but-preserve-me\n"
    history.write_text(old_line + recent_line + malformed, encoding="utf-8")

    summaries = summarize_codex_input_history(history, now=_NOW)
    assert [(item.cutoff_days, item.removable_records) for item in summaries] == [
        (30, 1),
        (90, 1),
        (180, 0),
    ]

    monkeypatch.setattr(
        "devclean.core.codex_history.application_process_running",
        lambda _app_id: False,
    )
    result = prune_codex_input_history(history, 90, now=_NOW)

    assert result.removed_records == 1
    assert result.kept_records == 2
    remaining = history.read_text(encoding="utf-8")
    assert "old\"" not in remaining
    assert "new\"" in remaining
    assert malformed.strip() in remaining


def test_input_history_prune_refuses_while_codex_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "devclean.core.codex_history.application_process_running",
        lambda _app_id: True,
    )

    with pytest.raises(CodexHistoryError, match="running"):
        prune_codex_input_history(history, 90, now=_NOW)


def test_thread_delete_uses_codex_app_server_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".codex"
    _rollout(home, _UUID_OLD, days_old=120)
    sessions = scan_codex_sessions(home)
    monkeypatch.setattr(
        "devclean.core.codex_history.application_process_running",
        lambda _app_id: False,
    )

    server = r'''
import json, sys
initialized = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"id": msg["id"], "result": {"codexHome": "fake"}}), flush=True)
    elif method == "initialized":
        initialized = True
    elif method == "thread/delete":
        if not initialized:
            print(json.dumps({"id": msg["id"], "error": {"message": "Not initialized"}}), flush=True)
        elif msg.get("params", {}).get("threadId") != "11111111-1111-4111-8111-111111111111":
            print(json.dumps({"id": msg["id"], "error": {"message": "wrong thread id"}}), flush=True)
        else:
            print(json.dumps({"method": "thread/deleted", "params": {"threadId": msg["params"]["threadId"]}}), flush=True)
            print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
'''

    def fake_popen(_args: object, **kwargs: Any) -> subprocess.Popen[str]:
        return cast(
            subprocess.Popen[str],
            subprocess.Popen(
                [sys.executable, "-u", "-c", server],
                **kwargs,
            ),
        )

    result = delete_codex_threads(
        sessions,
        home=home,
        codex_bin=Path("fake-codex"),
        popen_factory=fake_popen,
    )

    assert result.requested == 1
    assert result.deleted_or_already_absent == 1
    assert result.failed == ()


def test_thread_delete_never_falls_back_to_raw_delete_on_vendor_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / ".codex"
    path = _rollout(home, _UUID_OLD, days_old=120)
    sessions = scan_codex_sessions(home)
    monkeypatch.setattr(
        "devclean.core.codex_history.application_process_running",
        lambda _app_id: False,
    )
    server = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" in msg:
        if msg.get("method") == "initialize":
            print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
        elif msg.get("method") == "thread/delete":
            print(json.dumps({"id": msg["id"], "error": {"message": "referenced by another thread"}}), flush=True)
'''

    def fake_popen(_args: object, **kwargs: Any) -> subprocess.Popen[str]:
        return cast(
            subprocess.Popen[str],
            subprocess.Popen(
                [sys.executable, "-u", "-c", server],
                **kwargs,
            ),
        )

    result = delete_codex_threads(
        sessions,
        home=home,
        codex_bin=Path("fake-codex"),
        popen_factory=fake_popen,
    )

    assert result.deleted_or_already_absent == 0
    assert result.failed and "referenced" in result.failed[0][1]
    assert path.exists(), "DevClean must never raw-delete a thread Codex refused"
