"""Domain models and state management."""

# Install source-specific rule extensions before shared facades snapshot their
# callables. Keep Codex additions first because Claude's plugin extension imports
# the application facade after its own matcher is installed.
from devclean.core import codex_log_cleanup as _codex_log_cleanup  # noqa: F401
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
from devclean.core import claude_plugin_cache_cleanup as _claude_plugin_cache_cleanup  # noqa: F401
