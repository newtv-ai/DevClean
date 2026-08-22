"""Domain models and state management."""

# Install source-specific rule extensions before shared facades snapshot their
# callables. This keeps native Claude updater and plugin-staging authority in the
# normal scan path without turning the whole plugin cache into generic junk.
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
from devclean.core import claude_plugin_cache_cleanup as _claude_plugin_cache_cleanup  # noqa: F401
