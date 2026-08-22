"""Domain models and state management."""

# Install source-specific rule extensions and one-time product-default migrations
# before shared facades snapshot their callables.
from devclean.core import claude_native_cleanup as _claude_native_cleanup  # noqa: F401
from devclean.core import scan_scope_migration as _scan_scope_migration  # noqa: F401
