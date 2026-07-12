"""Policy boundary for observational vendor CLI commands."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from reclaimer.core.models import EffectClass
from reclaimer.platform.windows.process import (
    BoundedProcessResult,
    ProcessLimits,
    run_bounded_process,
    validate_executable_path,
)
from reclaimer.platform.windows.security import is_process_elevated

_ADAPTER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INHERITED_ENV = (
    "APPDATA",
    "CONDA_PKGS_DIRS",
    "HF_ASSETS_CACHE",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HF_XET_CACHE",
    "HOME",
    "LOCALAPPDATA",
    "NPM_CONFIG_CACHE",
    "PATHEXT",
    "PIP_CACHE_DIR",
    "PNPM_HOME",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "UV_CACHE_DIR",
    "WINDIR",
    "XDG_CACHE_HOME",
)
_SAFE_OVERRIDE_KEYS = frozenset(
    {
        "CONDA_REPORT_ERRORS",
        "CONDA_NO_PLUGINS",
        "CONDA_NOTIFY_OUTDATED_CONDA",
        "CONDA_OFFLINE",
        "CI",
        "DO_NOT_TRACK",
        "HF_HUB_DISABLE_IMPLICIT_TOKEN",
        "HF_HUB_DISABLE_PROGRESS_BARS",
        "HF_HUB_DISABLE_TELEMETRY",
        "HF_HUB_DISABLE_UPDATE_CHECK",
        "HF_HUB_OFFLINE",
        "NO_COLOR",
        "NO_UPDATE_NOTIFIER",
        "NPM_CONFIG_AUDIT",
        "NPM_CONFIG_FUND",
        "NPM_CONFIG_OFFLINE",
        "NPM_CONFIG_UPDATE_NOTIFIER",
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_NO_INDEX",
        "PIP_NO_INPUT",
        "PIP_NO_COLOR",
        "PNPM_CONFIG_OFFLINE",
        "PNPM_CONFIG_UPDATE_NOTIFIER",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "TERM",
        "UV_NO_PROGRESS",
        "UV_OFFLINE",
        "UV_PYTHON_DOWNLOADS",
    }
)


@dataclass(frozen=True, slots=True)
class QueryCommand:
    adapter_id: str
    executable: Path
    arguments: tuple[str, ...]
    effect_class: EffectClass
    limits: ProcessLimits = field(default_factory=ProcessLimits)
    environment_overrides: tuple[tuple[str, str], ...] = ()
    known_operational_writes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ADAPTER_ID.fullmatch(self.adapter_id):
            raise ValueError("invalid adapter_id")
        if self.effect_class not in {
            EffectClass.PURE_QUERY,
            EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        }:
            raise ValueError("query commands cannot be maintenance or destructive")
        object.__setattr__(self, "executable", validate_executable_path(self.executable))
        if any(not argument or "\x00" in argument for argument in self.arguments):
            raise ValueError("query arguments must be non-empty and contain no NUL")
        keys = [key for key, _ in self.environment_overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("environment override keys must be unique")
        unsupported = set(keys) - _SAFE_OVERRIDE_KEYS
        if unsupported:
            raise ValueError(f"unsupported environment overrides: {sorted(unsupported)}")

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


def run_query(command: QueryCommand) -> BoundedProcessResult:
    """Run a statically registered query command from a non-elevated process."""

    if is_process_elevated():
        raise PermissionError("vendor queries must not run from an elevated main process")
    environment = build_query_environment(
        command.executable, dict(command.environment_overrides)
    )
    return run_bounded_process(
        command.argv,
        environment=environment,
        cwd=safe_query_cwd(),
        limits=command.limits,
    )


def build_query_environment(
    executable: Path, overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build an allowlisted environment with proxy and credential variables excluded."""

    environment = {key: os.environ[key] for key in _INHERITED_ENV if key in os.environ}
    system_root = Path(environment.get("SYSTEMROOT", r"C:\Windows"))
    search_paths = (executable.parent, system_root / "System32", system_root)
    environment["PATH"] = os.pathsep.join(str(path) for path in search_paths)
    environment.update(
        {
            "DO_NOT_TRACK": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "NO_COLOR": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "TERM": "dumb",
            "UV_NO_PROGRESS": "1",
        }
    )
    for key, value in (overrides or {}).items():
        if key not in _SAFE_OVERRIDE_KEYS:
            raise ValueError(f"unsupported environment override: {key}")
        if "\x00" in value:
            raise ValueError("environment values must contain no NUL")
        environment[key] = value
    return environment


def safe_query_cwd() -> Path:
    system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    if system_root.is_absolute() and system_root.is_dir():
        return system_root
    return Path.home()


def decode_utf8(result: BoundedProcessResult) -> str:
    """Decode deterministic adapter output; replacement would hide parser corruption."""

    return result.stdout.decode("utf-8-sig", errors="strict")


__all__ = [
    "QueryCommand",
    "build_query_environment",
    "decode_utf8",
    "run_query",
    "safe_query_cwd",
]
