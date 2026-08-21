# Full rule re-audit tracker — 2026-08

This tracker exists so the requested second-pass audit is genuinely one-by-one rather than a sequence of ad-hoc fixes. A check means the layer has been re-audited on current main; it does not mean every neighboring product has already been re-verified.

## Cross-cutting pipeline

| Layer | Status | Result |
| --- | --- | --- |
| Packaged DELETE/KEEP defaults | ✅ phase 6 | raw machine history stays local; current audited product FILE rules overlay sidecars as a separate source |
| Generic file-name/suffix/cache heuristics | ✅ phase 2 | protected/report-only; no USER/AI delete authority |
| Generic directory-name/version heuristics | ✅ phase 2 | protected/report-only; no whole-tree USER delete authority |
| Generic unknown-file routing | ✅ phase 2 | protected; no default paid AI route |
| Legacy MANUAL_REVIEW raw roots | ✅ phase 2 | runtime fails closed to REPORT_ONLY |
| Static VENDOR_MANAGED root fallback | ✅ phase 3 | static roots are discovery-only; deterministic vendor authority requires an attached audited TOOL whole-tree rule |
| AGE_BASED_REVIEW temp lifecycle | ✅ phase 4 | raw mtime/age authority removed; legacy AGE roots fail closed and packaged temp/crash roots are discovery-only |
| Scan exclusions/pruning | ✅ phase 5 | explicit/audited nested roots survive skipped-name ancestors; exclusions and descendant pruning still win |
| Learned-rule target boundary | ✅ phase 2 | learned/default rules apply to files only; directory choices are exact-path-only and subtree KEEP wins |
| Learned-rule portability/default restoration | ✅ phase 6 | restored only source-supported common FILE knowledge; rejected personal/history/install-state and already-covered app-cache guesses |
| Execution identity/reparse/hardlink/concurrency gates | ✅ phase 7 | nested junction replacement TOCTOU closed with per-directory/per-leaf handle confinement; existing snapshot/link/share guards retained |

## Packaged known cleanup roots

| Root id | Current packaged policy | Re-audit state |
| --- | --- | --- |
| `user-temp` | `REPORT_ONLY` | phase 4: Storage Sense semantics do not justify raw mtime deletion authority |
| `windows-temp` | `REPORT_ONLY` | phase 4: Windows temp is discovery-only unless a narrower source-owned lane applies |
| `user-crash-dumps` | `REPORT_ONLY` | phase 4: generic age removed; exact Windows crash-dump USER_REVIEW lane remains source-specific |
| `system-crash-dumps` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-maintenance` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-update-downloads` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-old` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-internet-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `thumbnail-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `pip-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `uv-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `npm-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `pnpm-store` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `conda-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `huggingface-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `gradle-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `yarn-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `bun-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `maven-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `nuget-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `go-module-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `cargo-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `test-browser-binaries` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `android-images` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ollama` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ollama-updates` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `lmstudio` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `editor-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `jetbrains-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `browser-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ide-working-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `claude-plugin-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `general-tool-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |

## Application/source modules

Re-verify on current primary vendor docs/source in small PRs. Recent audits are evidence, not a permanent exemption from this second pass.

| Family | Status |
| --- | --- |
| Chromium browsers: Chrome / Edge / Brave / Vivaldi / Opera | ✅ re-verified (Chrome/Edge updater diagnostics protected; Brave/Vivaldi/Opera source-first authority corrections reconciled; narrow Chromium-derived cache lanes retained without widening) |
| Firefox | ✅ re-verified (profile/local-cache boundary retained; pending crash reports and updater logs protected) |
| Electron/editors: VS Code / Cursor / Windsurf / Trae / Claude / Codex | ✅ re-verified (product-specific USER/KEEP state retained; only exact source-identified Electron/runtime/vendor-maintenance lanes keep positive authority) |
| JetBrains / Toolbox / Android Studio | ✅ re-verified (mixed config/system/Local History/tool state remains protected; exact cache and vendor-expired old-version lanes stay narrowly source-bounded) |
| Python: pip / uv / Conda / PyTorch Hub / Hugging Face Hub | ✅ re-verified (vendor-owned prune/clean/remove operations used only with exact identity/scope proof; environments, credentials, models and ambiguous cache provenance remain protected/report-only) |
| JS: npm / pnpm / Yarn / Bun / Cypress / Playwright / Puppeteer | ✅ re-verified (vendor-maintenance lanes retained only where exact scope is provable; configuration-sensitive/shared caches remain protected/report-only) |
| JVM: Gradle / Maven | ✅ re-verified (Gradle User Home and Maven local repository stay protected/report-only; project clean execution remains bounded by complete vendor scope proof) |
| .NET / NuGet | ⏳ queued |
| Go / Cargo / Conan / vcpkg | ⏳ queued |
| Docker / Podman / WSL | ✅ re-verified (local-daemon/machine/distro boundaries retained; exact vendor operations only; volumes/VHD/shared or broad prune state remains protected/report-only) |
| Android SDK / AVD | ✅ re-verified (exact sdkmanager package identity plus strict AVD/system-image correlation retained; incomplete reference proof fails closed) |
| Unity / Unreal | ✅ re-verified (Unity storage stays split by project/package/vendor-managed semantics; Unreal DDC uses engine-owned maintenance; no raw Zen/DDC widening) |
| Ollama / LM Studio and other local-model products | ⏳ queued |
| Windows diagnostics / servicing / Recycle Bin / previous install | ✅ re-verified (servicing/rollback/recoverable/diagnostic semantics remain separate; exact vendor APIs/commands or exact-file USER_REVIEW only; broad raw cleanup stays protected) |
| Project build systems: Bazel / Cargo / Meson / CMake / MSBuild / Ninja / Make / SCons | ⏳ queued |

## Release packaging

| Surface | Status | Result |
| --- | --- | --- |
| Windows EXE bundled-runtime notices | ✅ re-audited | CPython aggregate notice already contains both Tcl and Tk terms; the redundant Tk-only sidecar is now accurately named |
| Checked-in `release/DevClean.exe` | ✅ retained | sidecar-only license-label correction leaves the accepted executable unchanged; exact-head CI must independently rebuild the Windows artifact |

## Acceptance rule for reducing user/AI burden

A reduction in USER_REVIEW or AI_REVIEW counts is accepted by moving an item to a source-proven exact deterministic lane, to protected/report-only, or by applying a separately confirmed reusable **file-level** DELETE/KEEP rule. Cache-like names, age, size, redownloadability, or a one-off AI guess are not authority by themselves; learned file authority never extends to directories or hard semantic protections.