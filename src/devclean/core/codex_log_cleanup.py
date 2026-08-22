"""Source-backed cleanup for Codex legacy append-only TUI logs.

Current Codex source removes ``$CODEX_HOME/log/codex-tui.log`` on TUI startup
because the old shared append-only log can grow without bound. DevClean can use
that vendor behavior as deterministic deletion authority instead of waiting for
an arbitrary age/size threshold or asking the user what the file means.

The rule is intentionally exact. Other files under ``$CODEX_HOME/log`` and the
SQLite runtime databases keep their existing semantics and are not broadened by
this extension.
"""

from __future__ import annotations

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


def install() -> None:
    if any(rule.rule_id == _LEGACY_TUI_LOG_RULE.rule_id for rule in _impl.CODEX_RULES):
        return
    _impl.CODEX_RULES = (*_impl.CODEX_RULES, _LEGACY_TUI_LOG_RULE)


install()


__all__ = ["install"]
