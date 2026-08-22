"""Domain models and state management."""

# ruff: noqa: I001
# Install source-specific rule extensions before shared facades snapshot their
# callables. This order is semantic: Codex must install before Claude's plugin
# extension imports the shared application facade and snapshots CODEX_RULES.
from devclean.core import codex_log_cleanup as _codex_log_cleanup  # noqa: F401
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
from devclean.core import claude_plugin_cache_cleanup as _claude_plugin_cache_cleanup  # noqa: F401
