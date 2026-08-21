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
| Chromium browsers: Chrome / Edge / Brave / Vivaldi / Opera | ⏳ queued (Brave/Vivaldi/Opera recent authority corrections already landed) |
| Firefox | ⏳ queued |
| Electron/editors: VS Code / Cursor / Windsurf / Trae / Claude / Codex | ⏳ queued |
| JetBrains / Toolbox / Android Studio | ⏳ queued |
| Python: pip / uv / Conda / PyTorch Hub / Hugging Face Hub | ⏳ queued |
| JS: npm / pnpm / Yarn / Bun / Cypress / Playwright / Puppeteer | ⏳ queued |
| JVM: Gradle / Maven | ⏳ queued |
| .NET / NuGet | ⏳ queued |
| Go / Cargo / Conan / vcpkg | ⏳ queued |
| Docker / Podman / WSL | ⏳ queued |
| Android SDK / AVD | ⏳ queued |
| Unity / Unreal | ⏳ queued |
| Ollama / LM Studio and other local-model products | ⏳ queued |
| Windows diagnostics / servicing / Recycle Bin / previous install | ⏳ queued |
| Project build systems: Bazel / Cargo / Meson / CMake / MSBuild / Ninja / Make / SCons | ⏳ queued |

## Acceptance rule for reducing user/AI burden

A reduction in USER_REVIEW or AI_REVIEW counts is accepted by moving an item to a source-proven exact deterministic lane, to protected/report-only, or by applying a separately confirmed reusable **file-level** DELETE/KEEP rule. Cache-like names, age, size, redownloadability, or a one-off AI guess are not authority by themselves; learned file authority never extends to directories or hard semantic protections.
