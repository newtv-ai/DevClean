"""Bounded current-format index for AI review exports."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from devclean.core.paths import data_dir

_INDEX_NAME: Final = "ai-sessions.json"
_SCHEMA: Final = 2
_MAX_EXPORT_SESSIONS: Final = 1_024
_MAX_EXPORTED_PATHS: Final = 100_000
_SAFE_SESSION_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SAFE_NONCE: Final = re.compile(r"[0-9a-f]{64}")
_SAFE_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ExportSession:
    nonce: str
    package_digest: str
    candidate_paths: dict[str, str]
    candidate_members: dict[str, tuple[str, ...]]


def remember_export(
    session_id: str,
    nonce: str,
    package_digest: str,
    candidate_paths: dict[str, str],
    candidate_members: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """Remember the small binding needed for strict restart imports."""

    if (
        _SAFE_SESSION_ID.fullmatch(session_id) is None
        or _SAFE_NONCE.fullmatch(nonce) is None
        or _SAFE_DIGEST.fullmatch(package_digest) is None
        or not candidate_paths
    ):
        return
    members = candidate_members or {
        candidate_id: (path,)
        for candidate_id, path in candidate_paths.items()
    }
    if (
        set(members) != set(candidate_paths)
        or any(
            not group
            or group[0] != candidate_paths[candidate_id]
            or any(not isinstance(path, str) or not path for path in group)
            for candidate_id, group in members.items()
        )
        or len({path for group in members.values() for path in group})
        != sum(len(group) for group in members.values())
    ):
        return
    sessions = _load()
    sessions[session_id] = {
        "updated_at": datetime.now(UTC).isoformat(),
        "nonce": nonce,
        "package_digest": package_digest,
        "paths": candidate_paths,
        "members": {
            candidate_id: list(group)
            for candidate_id, group in members.items()
        },
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


def recall_export(session_id: str) -> ExportSession | None:
    """Return the current-format binding for one exported volume."""

    if _SAFE_SESSION_ID.fullmatch(session_id) is None:
        return None
    record = _load().get(session_id)
    if not isinstance(record, dict):
        return None
    nonce = record.get("nonce")
    package_digest = record.get("package_digest")
    paths = record.get("paths")
    raw_members = record.get("members")
    if (
        not isinstance(nonce, str)
        or _SAFE_NONCE.fullmatch(nonce) is None
        or not isinstance(package_digest, str)
        or _SAFE_DIGEST.fullmatch(package_digest) is None
        or not isinstance(paths, dict)
    ):
        return None
    candidate_paths = {
        key: value
        for key, value in paths.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    if len(candidate_paths) != len(paths) or not candidate_paths:
        return None
    candidate_members: dict[str, tuple[str, ...]] = {
        candidate_id: (path,)
        for candidate_id, path in candidate_paths.items()
    }
    if raw_members is not None:
        if not isinstance(raw_members, dict) or set(raw_members) != set(candidate_paths):
            return None
        parsed_members: dict[str, tuple[str, ...]] = {}
        for candidate_id, raw_group in raw_members.items():
            if (
                not isinstance(candidate_id, str)
                or not isinstance(raw_group, list)
                or not raw_group
                or any(not isinstance(path, str) or not path for path in raw_group)
                or raw_group[0] != candidate_paths[candidate_id]
            ):
                return None
            parsed_members[candidate_id] = tuple(raw_group)
        if len(
            {
                path
                for group in parsed_members.values()
                for path in group
            }
        ) != sum(len(group) for group in parsed_members.values()):
            return None
        candidate_members = parsed_members
    return ExportSession(
        nonce=nonce,
        package_digest=package_digest,
        candidate_paths=candidate_paths,
        candidate_members=candidate_members,
    )


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
    members = record.get("members")
    if isinstance(members, dict):
        return sum(
            len(group)
            for group in members.values()
            if isinstance(group, list)
        )
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


__all__ = ["ExportSession", "forget_export", "recall_export", "remember_export"]
