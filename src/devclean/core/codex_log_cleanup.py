"""Source-backed cleanup for Codex log storage.

Current Codex source gives two different log stores different semantics:

* ``$CODEX_HOME/log/codex-tui.log`` is a legacy shared append-only log that the
  TUI now removes on startup because it can grow without bound. That exact file
  is deterministic cleanup residue.
* ``logs_2.sqlite`` is the current runtime log database. Codex keeps recent rows
  for feedback/diagnostics, prunes rows older than 10 days, and manages the DB in
  WAL mode. Treating ``logs_2.sqlite*`` as three independent delete candidates
  can split the SQLite database/WAL/SHM family and is not an acceptable mutation
  boundary.

Until DevClean has one atomic, source-aware maintenance action for the complete
runtime log DB family, those files remain protected. This is an execution-boundary
fix, not an assertion that the database can never contain reclaimable space.
"""

from __future__ import annotations

from dataclasses import replace

from devclean.core import _application_cleanup_impl as _impl
from devclean.core._application_cleanup_impl import (
    ApplicationCleanupRule,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    RebuildCost,
)

_LEGACY_TUI_LOG_RULE = ApplicationCleanupRule(
    rule_id="codex-legacy-tui-log",
    app_id="codex",
    root_key="CODEX_HOME",
    relative_pattern=r"log\codex-tui.log",
    match_kind=MatchKind.EXACT,
    owner=DecisionOwner.TOOL,
    last_use=LastUseStrategy.FILE_MTIME,
    rebuild_cost=RebuildCost.NONE,
    idle_days=0,
    min_reclaim_bytes=0,
    requires_process_closed=True,
    size_sensitive_idle=False,
    user_age_buckets=(),
    allow_whole_tree=False,
    label="Codex 已废弃且会在启动时自行删除的共享 TUI 日志",
)


def _protect_runtime_log_database(
    rules: tuple[ApplicationCleanupRule, ...],
) -> tuple[ApplicationCleanupRule, ...]:
    protected: list[ApplicationCleanupRule] = []
    for rule in rules:
        if rule.rule_id != "codex-log-db":
            protected.append(rule)
            continue
        protected.append(
            replace(
                rule,
                owner=DecisionOwner.KEEP,
                idle_days=None,
                min_reclaim_bytes=0,
                size_sensitive_idle=False,
                label="Codex 当前运行日志数据库 - 需按 SQLite 文件族原子维护",
            )
        )
    return tuple(protected)


def install() -> None:
    rules = _protect_runtime_log_database(_impl.CODEX_RULES)
    if not any(rule.rule_id == _LEGACY_TUI_LOG_RULE.rule_id for rule in rules):
        rules = (*rules, _LEGACY_TUI_LOG_RULE)
    _impl.CODEX_RULES = rules


install()


__all__ = ["install"]
