# Cypress storage audit sources

Audited 2026-08-17 against current official Cypress documentation.

- Advanced installation: https://docs.cypress.io/app/references/advanced-installation
  - Windows binary cache defaults to `%LOCALAPPDATA%\Cypress\Cache`.
  - `CYPRESS_CACHE_FOLDER` relocates the binary cache and expands `~` to the user's home.
  - Cypress binary storage is global and shared between projects.
  - Cypress App Data is separate at `%APPDATA%\Cypress` on Windows.
  - `CYPRESS_RUN_BINARY` selects an already-unzipped runtime outside normal cache resolution.
- Command-line reference: https://docs.cypress.io/app/references/command-line
  - `cypress cache path` reports the effective cache.
  - `cypress cache list --size` reports cached versions, last-used information and sizes.
  - `cypress cache prune` removes all cached versions except the version currently installed for the invoking Cypress package.
  - `cypress cache clear` removes every cached Cypress binary.
- CI overview: https://docs.cypress.io/app/continuous-integration/overview
  - Cypress caches are commonly shared/restored across project runs and can accumulate multiple versions.

DevClean conclusion: the global Cypress binary cache is useful inventory but is not granted unattended mutation authority. A prune run from one project does not establish that binaries required by other projects are unused, and cache clear is intentionally destructive to every installed version.
