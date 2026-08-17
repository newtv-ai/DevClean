# Playwright browser storage audit

DevClean treats Playwright's browser registry as vendor-managed runtime payload, not as a folder-shaped cache.

- On Windows, the shared browser registry defaults to `%LOCALAPPDATA%\ms-playwright`.
- An absolute `PLAYWRIGHT_BROWSERS_PATH` is honored as the effective shared registry.
- `PLAYWRIGHT_BROWSERS_PATH=0` means package-local `.local-browsers`; DevClean deliberately does not guess project/package roots in this mode.
- Relative browser-path overrides are not resolved against an invented working directory and therefore are not scanned.
- Playwright tracks clients and automatically removes stale browser versions when no clients require them. `PLAYWRIGHT_SKIP_BROWSER_GC=1` explicitly disables that vendor garbage collection.
- The shared registry remains KEEP / REPORT_ONLY regardless of age or size; DevClean grants no raw file or whole-tree deletion authority.
- `playwright uninstall` / `playwright uninstall --all` are deliberately not automated here because they can remove browser builds still required by active Playwright installations and force a later redownload.
