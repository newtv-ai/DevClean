"""Every running process with its program path and command line, in one query.

One PowerShell CIM query lists all processes. Callers share its answer for a
whole scan or cleanup run instead of asking once per application or per file.
Output is UTF-8 JSON so paths in any language survive. A failed query raises:
"could not tell" must never read as "nothing is running".
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | "
    "Select-Object Name,SessionId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
)


class ProcessListError(OSError):
    """The process list could not be read."""


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """One running process; Windows hides path and command line of some processes.

    ``session_id`` 0 is the services session: only Windows and services can
    start programs there. ``None`` means Windows did not say.
    """

    name: str
    executable: str
    command_line: str
    session_id: int | None = None


def running_processes() -> tuple[ProcessInfo, ...]:
    """List running processes; raise :class:`ProcessListError` when that fails."""

    if os.name != "nt":
        return ()
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProcessListError(f"process query could not run: {error}") from error
    if result.returncode != 0:
        raise ProcessListError(f"process query failed with exit code {result.returncode}")
    return parse_process_list(result.stdout)


def parse_process_list(text: str) -> tuple[ProcessInfo, ...]:
    """Parse the query's JSON (one object or an array of objects)."""

    try:
        data = json.loads(text)
    except ValueError as error:
        raise ProcessListError("process query returned unreadable output") from error
    rows = data if isinstance(data, list) else [data]
    processes: list[ProcessInfo] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Name"), str):
            raise ProcessListError("process query returned an unexpected row")
        session = row.get("SessionId")
        processes.append(
            ProcessInfo(
                name=row["Name"],
                executable=row.get("ExecutablePath") or "",
                command_line=row.get("CommandLine") or "",
                session_id=(
                    session
                    if isinstance(session, int) and not isinstance(session, bool)
                    else None
                ),
            )
        )
    if not processes:
        raise ProcessListError("process query returned no processes")
    return tuple(processes)


__all__ = ["ProcessInfo", "ProcessListError", "parse_process_list", "running_processes"]
