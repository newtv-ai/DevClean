"""Domain models and state management."""

# Install source-specific rule extensions before the shared application cleanup
# facade imports and snapshots their callables.
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
