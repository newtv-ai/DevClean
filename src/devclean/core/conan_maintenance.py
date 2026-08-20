"""Conan 2 cache inventory and vendor-supported non-critical cleanup.

Conan explicitly treats its package storage as read-only for callers and exposes
``conan cache clean`` for deleting generated source, build, download and
temporary folders. DevClean therefore never edits Conan's cache directly.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_GIB = 1024**3
_RECOMMEND_BYTES = _GIB
_VERSION_RE = re.compile(r"(?:Conan\s+version\s+)?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConanStorageInventory:
    home: Path
    logical_bytes: int
    exists: bool
    version: str
    recommended: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ConanCacheCleanResult:
    home: Path
    before_bytes: int
    after_bytes: int
    command: tuple[str, ...]
    output: str

    @property
    def reclaimed_bytes(self) -> int:
        return max(0, self.before_bytes - self.after_bytes)


def inventory_conan_storage(
    environment: Mapping[str, str] | None = None,
) -> ConanStorageInventory:
    """Resolve Conan 2's active home through Conan itself and inventory it read-only."""

    executable = conan_executable(environment)
    version = _require_conan2(executable, environment)
    home = _reported_conan_home(executable, environment)
    try:
        exists = home.is_dir()
    except OSError:
        exists = False
    size = _directory_bytes(home) if exists else 0
    return ConanStorageInventory(
        home=home,
        logical_bytes=size,
        exists=exists,
        version=version,
        recommended=exists and size >= _RECOMMEND_BYTES,
        reason=(
            "Conan 2 can safely remove its generated source/build/download/temp folders "
            "with conan cache clean; installed package artifacts remain in the cache"
        ),
    )


def clean_conan_cache(
    home: Path,
    environment: Mapping[str, str] | None = None,
) -> ConanCacheCleanResult:
    """Clean only Conan-defined non-critical cache folders for the active cache."""

    clear_conan_process_cache()
    executable = conan_executable(environment)
    _require_conan2(executable, environment)
    reported_home = _reported_conan_home(executable, environment)
    if _normalized(home) != _normalized(reported_home):
        raise ValueError(
            f"所选目录不是当前 Conan 2 home: selected={home}, reported={reported_home}"
        )
    if conan_process_running():
        raise RuntimeError("Conan 正在运行; 请等待包管理或构建操作完成后再清理")
    if not reported_home.is_dir():
        raise FileNotFoundError(f"Conan 2 home 不存在: {reported_home}")

    before = _directory_bytes(reported_home)
    command = (
        executable,
        "cache",
        "clean",
        "*",
        "-cc",
        "core:non_interactive=True",
    )
    result = _run_conan(command, environment, timeout=900)
    after = _directory_bytes(reported_home)
    output = (result.stdout or result.stderr).strip()
    return ConanCacheCleanResult(
        home=reported_home,
        before_bytes=before,
        after_bytes=after,
        command=command,
        output=output,
    )


def conan_executable(environment: Mapping[str, str] | None = None) -> str:
    env = _casefold_env(environment)
    configured = env.get("devclean_conan_exe")
    if configured:
        return configured
    return "conan.exe" if os.name == "nt" else "conan"


@lru_cache(maxsize=1)
def conan_process_running() -> bool:
    if os.name != "nt":
        return False
    script = (
        "$p=Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -ieq 'conan.exe' -or "
        "(($_.Name -match '(?i)^(?:python|pythonw|py)(?:\\d+(?:\\.\\d+)?)?\\.exe$') "
        "-and $_.CommandLine -match '(?i)(?:^|\\s)-m\\s+conan(?:\\s|$)') }; "
        "if ($p) { 'RUNNING' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or "RUNNING" in result.stdout


def clear_conan_process_cache() -> None:
    conan_process_running.cache_clear()


def _require_conan2(
    executable: str,
    environment: Mapping[str, str] | None,
) -> str:
    result = _run_conan((executable, "--version"), environment, timeout=15)
    text = (result.stdout or result.stderr).strip()
    match = _VERSION_RE.search(text)
    if match is None:
        raise RuntimeError(f"无法识别 Conan 版本: {text or 'empty output'}")
    if int(match.group(1)) < 2:
        raise RuntimeError(f"需要 Conan 2.x, 当前版本为: {text}")
    return text


def _reported_conan_home(
    executable: str,
    environment: Mapping[str, str] | None,
) -> Path:
    result = _run_conan((executable, "config", "home"), environment, timeout=30)
    lines = [line.strip().strip('"').strip("'") for line in result.stdout.splitlines()]
    candidates = [line for line in lines if line]
    if not candidates:
        raise RuntimeError("Conan config home 没有返回有效路径")
    home = Path(candidates[-1]).expanduser()
    if not home.is_absolute():
        raise RuntimeError(f"Conan config home 返回了非绝对路径: {home}")
    return home


def _run_conan(
    command: tuple[str, ...],
    environment: Mapping[str, str] | None,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if environment is not None:
        env.update(environment)
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"无法执行 Conan CLI: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"Conan CLI 执行失败 (退出码 {result.returncode}): {detail}"
        )
    return result


def _directory_bytes(root: Path) -> int:
    total = 0
    try:
        for directory, _subdirs, files in os.walk(root):
            base = Path(directory)
            for name in files:
                try:
                    total += (base / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _casefold_env(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = [
    "ConanCacheCleanResult",
    "ConanStorageInventory",
    "clean_conan_cache",
    "clear_conan_process_cache",
    "conan_executable",
    "conan_process_running",
    "inventory_conan_storage",
]
