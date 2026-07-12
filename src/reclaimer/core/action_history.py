"""Small, redacted, bounded JSONL history for GUI file actions."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from reclaimer.core.paths import data_dir

_DEFAULT_MAX_BYTES = 1 * 1024 * 1024
_DEFAULT_MAX_ENTRIES = 2_000


@dataclass(frozen=True, slots=True)
class ActionEvent:
    action: str
    category: str
    path: str
    logical_size: int
    detail: str
    occurred_at: str

    @classmethod
    def create(
        cls, *, action: str, category: str, path: str, logical_size: int, detail: str
    ) -> ActionEvent:
        if logical_size < 0:
            raise ValueError("action history size must be non-negative")
        return cls(
            action=action,
            category=category,
            path=_redact_path(path),
            logical_size=logical_size,
            detail=detail[:240],
            occurred_at=datetime.now(UTC).isoformat(),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": "reclaimer-action-history-v1",
            "occurred_at": self.occurred_at,
            "action": self.action,
            "category": self.category,
            "path": self.path,
            "logical_size": self.logical_size,
            "detail": self.detail,
        }


class ActionHistory:
    """Append and read a capped user-local action history without SQLite."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        if max_bytes < 1 or max_entries < 1:
            raise ValueError("action history bounds must be positive")
        self.path = path or data_dir() / "history" / "actions.jsonl"
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._lock = threading.Lock()

    def append(self, event: ActionEvent) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event.payload(), ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > self.max_bytes:
                raise ValueError("single action history entry exceeds the history capacity")
            if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                backup = self.path.with_suffix(".previous.jsonl")
                if backup.exists():
                    backup.unlink()
                self.path.replace(backup)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)

    def recent(self) -> tuple[ActionEvent, ...]:
        with self._lock:
            if not self.path.is_file():
                return ()
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return ()
        events: list[ActionEvent] = []
        for line in lines[-self.max_entries :]:
            event = _parse_event(line)
            if event is not None:
                events.append(event)
        return tuple(reversed(events))


def _parse_event(line: str) -> ActionEvent | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != "reclaimer-action-history-v1":
        return None
    action = payload.get("action")
    category = payload.get("category")
    path = payload.get("path")
    detail = payload.get("detail")
    occurred_at = payload.get("occurred_at")
    size = payload.get("logical_size")
    if (
        not all(
            isinstance(value, str) and value
            for value in (action, category, path, detail, occurred_at)
        )
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        return None
    if not all(isinstance(value, str) for value in (action, category, path, detail, occurred_at)):
        return None
    return ActionEvent(
        cast(str, action),
        cast(str, category),
        cast(str, path),
        size,
        cast(str, detail),
        cast(str, occurred_at),
    )


def _redact_path(path: str) -> str:
    home = str(Path.home())
    normalized = os.path.normcase(path)
    if home and normalized.startswith(os.path.normcase(home)):
        return "<USER>" + path[len(home) :]
    return f"<EXTERNAL>\\{Path(path).name}"


__all__ = ["ActionEvent", "ActionHistory"]
