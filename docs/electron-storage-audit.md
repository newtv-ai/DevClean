# Electron download-cache audit

DevClean treats Electron's global download cache as mixed vendor/user-supplied artifact storage rather than granting generic recursive deletion authority.

Source-audited boundaries:

- Current Electron installation documentation states that `@electron/get` caches downloaded release artifacts under `%LOCALAPPDATA%\electron\Cache` on Windows, with `electron_config_cache` available as an override.
- Older Electron installations may also have cache data under `~/.electron`; DevClean inventories that legacy location separately.
- The cache contains Electron release ZIP archives and checksums and is used to avoid repeated network downloads.
- Electron explicitly documents that the cache can also be used to provide custom builds or to avoid contacting the network. Therefore a ZIP-looking file inside the cache is not proof that the artifact is a replaceable official download.
- Current `@electron/get` API documentation says the default read/write cache returns paths that point directly into the disk cache and callers should not move or delete those paths while using them.
- Electron exposes cache-bypass/redownload controls, but no source-audited vendor garbage-collection command that can distinguish official disposable artifacts from user-preseeded custom builds.
- A relative `electron_config_cache` is not resolved by DevClean against an invented npm/project working directory; only an absolute effective override is inventoried.

Conclusion: active and legacy Electron cache roots are `INSTALLERS_DOWNLOADS` / REPORT_ONLY / KEEP. DevClean grants zero raw file or whole-tree deletion authority. A future cleanup path would first need an authoritative way to prove an artifact is an official reproducible download rather than a custom user-provided build.

Official references audited on 2026-08-17:

- https://www.electronjs.org/docs/latest/tutorial/installation
- https://github.com/electron/get
- https://github.com/electron/get/blob/main/src/types.ts
