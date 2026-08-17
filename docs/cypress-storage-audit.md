# Cypress binary storage audit

DevClean treats Cypress's global binary cache as shared vendor-managed runtime payload, not as a generic folder-shaped cache.

Source-audited boundaries:

- Current Cypress documentation defines the Windows default binary cache as `%LOCALAPPDATA%\Cypress\Cache` and allows it to be moved with `CYPRESS_CACHE_FOLDER`.
- Cypress expands `~` in `CYPRESS_CACHE_FOLDER`; project-relative cache locations are also supported and are resolved from the Cypress invocation context. DevClean expands `~` from `USERPROFILE` but deliberately fails closed on relative values because it has no authoritative Cypress project working directory.
- Cypress configuration may arrive through the actual `CYPRESS_CACHE_FOLDER` environment variable or npm configuration environment variables. DevClean honors the documented environment precedence it can observe directly.
- The binary cache is global and intentionally shared by projects. It may contain several Cypress versions at once.
- `cypress cache list --size` can report installed versions, last-used information and size. `cypress cache clear` removes all cached binaries. `cypress cache prune` removes every cached binary except the version associated with the Cypress installation invoking the command.
- Because different projects on the same machine may intentionally use different Cypress versions, DevClean does not invoke `cache prune` or `cache clear` from the generic cleanup path. Retaining only the caller's current version would not prove that versions required by other projects are unused.
- `%APPDATA%\Cypress` is separate Cypress App Data. Although Cypress documents manual removal during full uninstall/troubleshooting, DevClean treats it as persistent KEEP state rather than assuming all files there are disposable.
- `CYPRESS_RUN_BINARY` can point at a user-selected runtime outside the global cache; that exact runtime path is protected as KEEP.

The discovered shared binary cache is therefore `TEST_BROWSER_BINARIES` / REPORT_ONLY with zero raw file or whole-tree deletion authority.

Official references audited on 2026-08-17:

- https://docs.cypress.io/app/references/advanced-installation
- https://docs.cypress.io/app/references/command-line
- https://docs.cypress.io/app/continuous-integration/overview
