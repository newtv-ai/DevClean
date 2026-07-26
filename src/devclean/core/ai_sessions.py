"""Bounded current-format index for AI review exports."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from devclean.core.paths import data_dir

_INDEX_NAME: Final = "ai-sessions.json"
_SCHEMA: Final = 1
_MAX_EXPORT_SESSIONS: Final = 1_024
_MAX_EXPORTED_PATHS: Final = 100_000
_SAFE_SESSION_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")


def remember_export(session_id: str, candidate_paths: dict[str, str]) -> None:
    """Remember which exact path each candidate id represents."""

    if _SAFE_SESSION_ID.fullmatch(session_id) is None or not candidate_paths:
        return
    sessions = _load()
    sessions[session_id] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "paths": candidate_paths,
    }
    ordered = sorted(
        sessions.items(),
        key=lambda pair: str(pair[1].get("updated_at", "")),
    )
    while ordered and (
        len(ordered) > _MAX_EXPORT_SESSIONS
        or sum(
            _record_path_count(record)
            for _key, record in ordered
        )
        > _MAX_EXPORTED_PATHS
    ):
        ordered.pop(0)
    _write(dict(ordered))


def recall_export(session_id: str) -> dict[str, str]:
    """Return the candidate-id/path map for a current-format export."""

    if _SAFE_SESSION_ID.fullmatch(session_id) is None:
        return {}
    record = _load().get(session_id)
    paths = record.get("paths") if isinstance(record, dict) else None
    if not isinstance(paths, dict):
        return {}
    return {
        key: value
        for key, value in paths.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def forget_export(session_id: str) -> None:
    """Remove one consumed export from the current index."""

    if _SAFE_SESSION_ID.fullmatch(session_id) is None:
        return
    sessions = _load()
    if session_id not in sessions:
        return
    del sessions[session_id]
    _write(sessions)


def _index_path() -> Path:
    return data_dir() / _INDEX_NAME


def _record_path_count(record: dict[str, object]) -> int:
    paths = record.get("paths")
    return len(paths) if isinstance(paths, dict) else 0


def _load() -> dict[str, dict[str, object]]:
    try:
        payload = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = (
        payload.get("sessions")
        if isinstance(payload, dict) and payload.get("schema") == _SCHEMA
        else None
    )
    if not isinstance(sessions, dict):
        return {}
    return {
        session_id: record
        for session_id, record in sessions.items()
        if isinstance(session_id, str)
        and _SAFE_SESSION_ID.fullmatch(session_id) is not None
        and isinstance(record, dict)
    }


def _write(sessions: dict[str, dict[str, object]]) -> None:
    target = _index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".writing")
    scratch.write_text(
        json.dumps(
            {
                "schema": _SCHEMA,
                "updated_at": datetime.now(UTC).isoformat(),
                "sessions": sessions,
            },
            ensure_ascii=False,
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(scratch, target)


__all__ = ["forget_export", "recall_export", "remember_export"]
