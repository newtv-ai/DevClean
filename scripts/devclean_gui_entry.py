"""PyInstaller entry point for the user-facing native Windows GUI."""

from devclean.platform.windows.subprocess_policy import install_no_console_subprocess_policy

# Install before importing the launcher: many source-audited root detectors use
# console vendor tools (PowerShell, npm.cmd, etc.) during GUI startup/scan setup.
install_no_console_subprocess_policy()

from devclean.ui import launcher  # noqa: E402
from devclean.ui.product_app import ProductDevCleanWindow  # noqa: E402

# Keep the launcher's existing advanced menus, but use the product window that
# adds visible scan timing without changing rule/AI classification semantics.
vars(launcher)["ModernDevCleanWindow"] = ProductDevCleanWindow

raise SystemExit(launcher.main())
