"""PyInstaller entry point for the user-facing native Windows GUI."""

from devclean.platform.windows.subprocess_policy import install_no_console_subprocess_policy

# Install before importing the launcher: many source-audited root detectors use
# console vendor tools (PowerShell, npm.cmd, etc.) during GUI startup/scan setup.
install_no_console_subprocess_policy()

from devclean.ui.launcher import main  # noqa: E402

raise SystemExit(main())
