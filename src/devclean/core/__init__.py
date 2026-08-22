"""Domain models and state management."""

# Install source-specific rule extensions before shared facades snapshot their
# callables. This keeps native Claude updater authority in the normal scan path.
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
