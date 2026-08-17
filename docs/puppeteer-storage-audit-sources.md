# Puppeteer storage audit sources

Audited 2026-08-17 against current official Puppeteer documentation.

- Configuration interface: https://pptr.dev/api/puppeteer.configuration
  - `cacheDirectory` defaults to `path.join(os.homedir(), '.cache', 'puppeteer')`.
  - `PUPPETEER_CACHE_DIR` overrides the configured cache directory.
- Configuration guide: https://pptr.dev/guides/configuration
  - browser downloads are globally cached under `~/.cache/puppeteer` by default.
  - project configuration can relocate `cacheDirectory`.
- Installation guide: https://pptr.dev/guides/installation
  - Puppeteer downloads Chrome for Testing and chrome-headless-shell into its cache by default.
- `trimCache()` API: https://pptr.dev/api/puppeteer.puppeteernode.trimcache
  - removes non-current Firefox and Chrome binaries for the calling Puppeteer configuration.
  - explicitly does not check whether other Puppeteer versions on the host sharing the cache require those binaries.
- `@puppeteer/browsers` CLI: https://pptr.dev/browsers-api
  - `clear` removes all installed browsers in the target cache.

DevClean conclusion: shared Puppeteer browser storage is inventory-only. Neither `trimCache()` nor `clear` is safe enough to expose as generic unattended cleanup because cache liveness can span multiple Puppeteer installations/versions.
