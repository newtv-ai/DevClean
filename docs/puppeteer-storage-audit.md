# Puppeteer browser storage audit

DevClean treats Puppeteer's downloaded browser cache as vendor-managed runtime payload, not as a folder-shaped cache that can be recursively deleted by generic cleanup.

Source-audited boundaries:

- Current Puppeteer configuration documents `cacheDirectory` with default `path.join(os.homedir(), '.cache', 'puppeteer')` and states that `PUPPETEER_CACHE_DIR` overrides it.
- Current installation/troubleshooting documentation confirms that Puppeteer downloads browser runtimes into `~/.cache/puppeteer` by default starting with v19.
- An absolute `PUPPETEER_CACHE_DIR` is honored by DevClean; a relative override is not resolved against an invented working directory and therefore fails closed.
- Project configuration files can relocate `cacheDirectory`; DevClean does not crawl arbitrary projects looking for `.puppeteerrc.*` or `puppeteer.config.*`, because those locations are project-defined and not globally authoritative.
- A single effective cache can be shared by multiple projects or Puppeteer package versions, so directory age and browser-version folder names are not authoritative liveness signals.
- `trimCache()` removes non-current Chrome/Firefox binaries according to the calling Puppeteer installation, and the official API explicitly warns that it does not check whether another Puppeteer version sharing the same cache still requires those binaries.
- `@puppeteer/browsers clear` clears all installed browsers in its target cache, which is too broad for generic cleanup.
- Therefore the discovered shared browser cache remains KEEP / `TEST_BROWSER_BINARIES` / REPORT_ONLY regardless of age or size, and DevClean grants no raw file or whole-tree deletion authority.

Official references audited on 2026-08-17:

- https://pptr.dev/api/puppeteer.configuration
- https://pptr.dev/guides/configuration
- https://pptr.dev/guides/installation
- https://pptr.dev/api/puppeteer.puppeteernode.trimcache
- https://pptr.dev/browsers-api
