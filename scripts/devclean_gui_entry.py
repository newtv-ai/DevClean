"""PyInstaller entry point for the user-facing native Windows GUI."""

from devclean.platform.windows.subprocess_policy import install_no_console_subprocess_policy

# Install before importing the launcher: source-audited root detectors may use
# console vendor tools while discovering installed caches.
install_no_console_subprocess_policy()

from devclean.ui.launcher import main  # noqa: E402

raise SystemExit(main())
