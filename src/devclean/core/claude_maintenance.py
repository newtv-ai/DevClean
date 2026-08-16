"""Vendor-aware maintenance actions that must not use the generic file purger."""

# ruff: noqa: RUF001

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from devclean.core.claude_cleanup import claude_process_running, claude_roots


class ClaudeMaintenanceError(RuntimeError):
    """A Claude vendor-managed maintenance action could not run safely."""


@dataclass(frozen=True, slots=True)
class ClaudePluginPruneResult:
    dry_run: bool
    command: tuple[str, ...]
    returncode: int
    output: str


RunFactory = Callable[..., subprocess.CompletedProcess[str]]


def resolve_claude_binary(environment: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environment is None else environment
    explicit = env.get("CLAUDE_BIN")
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
    for name in ("claude.exe", "claude"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def claude_plugin_storage_bytes(environment: Mapping[str, str] | None = None) -> int:
    """Return total installed plugin storage, not an exact prune estimate."""

    root = claude_roots(environment).plugins
    if root is None:
        return 0
    total = 0
    base = Path(str(root))
    try:
        iterator = base.rglob("*")
        for path in iterator:
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def run_claude_plugin_prune(
    *,
    dry_run: bool,
    environment: Mapping[str, str] | None = None,
    binary: Path | None = None,
    runner: RunFactory = subprocess.run,
) -> ClaudePluginPruneResult:
    """Run Claude's own orphan-dependency pruner at user scope.

    Directly installed plugins and persistent plugin data remain outside generic
    DevClean deletion authority. Claude Code itself decides which automatically
    installed dependencies are orphaned.
    """

    if claude_process_running():
        raise ClaudeMaintenanceError(
            "Claude Code 正在运行；关闭所有 Claude Code 窗口后再清理插件依赖"
        )
    executable = binary or resolve_claude_binary(environment)
    if executable is None:
        raise ClaudeMaintenanceError("未找到 claude/claude.exe，无法调用 plugin prune")
    command: list[str] = [
        str(executable),
        "plugin",
        "prune",
        "--scope",
        "user",
    ]
    if dry_run:
        command.append("--dry-run")
    else:
        command.append("--yes")
    try:
        completed = runner(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=dict(os.environ if environment is None else environment),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ClaudeMaintenanceError(f"Claude plugin prune 启动失败：{error}") from error
    output = _combined_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        raise ClaudeMaintenanceError(
            f"Claude plugin prune 返回 {completed.returncode}：{output or '没有输出'}"
        )
    return ClaudePluginPruneResult(
        dry_run=dry_run,
        command=tuple(command),
        returncode=completed.returncode,
        output=output,
    )


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    chunks: Sequence[str | None] = (stdout, stderr)
    return "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())


__all__ = [
    "ClaudeMaintenanceError",
    "ClaudePluginPruneResult",
    "claude_plugin_storage_bytes",
    "resolve_claude_binary",
    "run_claude_plugin_prune",
]
