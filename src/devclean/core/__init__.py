"""Domain models and state management."""

# ruff: noqa: I001
# Install source-specific rule extensions before shared facades snapshot their
# callables. This order is semantic: Codex must install before Claude's plugin
# extension imports the shared application facade and snapshots CODEX_RULES.
from devclean.core import codex_log_cleanup as _codex_log_cleanup  # noqa: F401
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
from devclean.core import claude_plugin_cache_cleanup as _claude_plugin_cache_cleanup  # noqa: F401

# Cursor's updater extension deliberately comes after the Claude plugin extension:
# that extension has loaded the application facade, and Cursor can then patch the
# facade's callable snapshots plus dynamic whole-tree discovery in one place.
from devclean.core import cursor_updater_cleanup as _cursor_updater_cleanup  # noqa: F401
# Cache semantics wrap the updater-aware Cursor evaluator: known generated caches
# stay safe regardless of age/size, while the stronger updater process guard remains.
from devclean.core import cursor_cache_semantics as _cursor_cache_semantics  # noqa: F401
# VS Code already exposes exact audited cache roots through the shared facade;
# this wrapper keeps file-level triage consistent with the same safety boundary.
from devclean.core import vscode_cache_semantics as _vscode_cache_semantics  # noqa: F401
# Chrome/Chromium uses the same rule: exact source-backed caches stay safe, while
# profile data and persistent site storage keep their existing protected lanes.
from devclean.core import chrome_cache_semantics as _chrome_cache_semantics  # noqa: F401
# Edge clones Chromium's audited browser-cache boundaries but keeps Microsoft
# updater state/logs separate; only those exact generated caches get this rule.
from devclean.core import edge_cache_semantics as _edge_cache_semantics  # noqa: F401
# Brave follows the same Chromium cache boundary while its Omaha updater working
# tree and state remain protected by Brave-specific KEEP rules.
from devclean.core import brave_cache_semantics as _brave_cache_semantics  # noqa: F401
