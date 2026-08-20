# Full rule re-audit tracker — 2026-08

This tracker exists so the requested second-pass audit is genuinely one-by-one rather than a sequence of ad-hoc fixes. A check means the layer has been re-audited on current main; it does not mean every neighboring product has already been re-verified.

## Cross-cutting pipeline

| Layer | Status | Result |
| --- | --- | --- |
| Packaged DELETE/KEEP defaults | ⚠ #142 interim | backup/migration bug fixed; defaults temporarily neutralized, safe file-level knowledge will be selectively restored |
| Generic file-name/suffix/cache heuristics | ✅ phase 2 | protected/report-only; no USER/AI delete authority |
| Generic directory-name/version heuristics | ✅ phase 2 | protected/report-only; no whole-tree USER delete authority |
| Generic unknown-file routing | ✅ phase 2 | protected; no default paid AI route |
| Legacy MANUAL_REVIEW raw roots | ✅ phase 2 | runtime fails closed to REPORT_ONLY |
| Static VENDOR_MANAGED root fallback | ✅ phase 3 | static roots are discovery-only; deterministic vendor authority requires an attached audited TOOL whole-tree rule |
| AGE_BASED_REVIEW temp lifecycle | ⏳ next | Microsoft Storage Sense semantics do not justify raw one-day mtime authority; rework this next |
| Scan exclusions/pruning | ⏳ queued | verify no important audited cache is accidentally skipped and no user data is widened |
| Learned-rule target boundary | ✅ phase 2 | learned/default rules apply to files only; directory choices are exact-path-only and subtree KEEP wins |
| Learned-rule portability/default restoration | ⏳ queued | re-audit old packaged file rules, generated glob/regex reuse, then selectively restore safe common-file knowledge |
| Execution identity/reparse/hardlink/concurrency gates | ⏳ queued | second-pass regression audit; no weakening planned |

## Packaged known cleanup roots

| Root id | Current packaged policy | Re-audit state |
| --- | --- | --- |
| `user-temp` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-temp` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `user-crash-dumps` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `system-crash-dumps` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-maintenance` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-update-downloads` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-old` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `windows-internet-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `thumbnail-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `pip-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `uv-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `npm-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `pnpm-store` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `conda-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `huggingface-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `gradle-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `yarn-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `bun-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `maven-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `nuget-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `go-module-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `cargo-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `test-browser-binaries` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `android-images` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ollama` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ollama-updates` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `lmstudio` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `editor-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `jetbrains-cache` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `browser-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `ide-working-caches` | `REPORT_ONLY` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
| `claude-plugin-cache` | `VENDOR_MANAGED` | phase 2 generic boundary applied; vendor/source detail still tracked separately |
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
