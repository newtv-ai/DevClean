# Android SDK installed-package maintenance audit

Audited: 2026-08-18

## Product conclusion

The Android SDK is installed developer tooling, not a disposable cache tree. Platforms, Build Tools, NDKs, CMake packages, emulator/system images, platform-tools, and command-line tools can all remain valuable because projects or AVDs may still depend on a specific installed version.

Android's official `sdkmanager` provides both the authoritative installed-package list and a supported per-package uninstall operation. DevClean therefore adds a narrow vendor-owned lane:

- inventory only package IDs that the selected SDK's own `sdkmanager --list` reports as installed;
- display exact version, description, reported location, and measured directory size;
- classify every uninstallable package as **USER_REVIEW**;
- invoke only `sdkmanager --uninstall <package> --sdk_root=<exact-root>` after fresh revalidation;
- never recursively delete an SDK component directory merely because its name looks old.

AI adds no value. The technical identity is known; whether a project still needs Android 34 vs 35, a particular NDK, system image, or Build Tools revision is user intent.

## SDK-root discovery

DevClean reuses the existing source-audited Android SDK-root resolver. Supported roots come from the documented Android environment/configuration conventions already used by the repository, rather than scanning the whole disk for folders named `Sdk`.

For each existing source-backed root DevClean looks for that SDK's own command-line tool in this order:

1. `cmdline-tools/latest/bin/sdkmanager(.bat)`;
2. other installed `cmdline-tools/<version>/bin/sdkmanager(.bat)` locations;
3. the legacy `tools/bin/sdkmanager(.bat)` location.

The executable must itself be a real file strictly below the selected SDK root. A root with no usable `sdkmanager` remains visible but receives no package-uninstall authority.

## Installed-package identity

Android's current command-line tools documentation defines `sdkmanager --list` as the operation that reports installed and available packages. Package identifiers use semicolon-separated SDK-style paths such as:

- `platforms;android-35`;
- `build-tools;35.0.0`;
- `system-images;android-35;google_apis;x86_64`.

DevClean parses only the **Installed packages** table. It does not promote entries from the Available Packages or Available Updates sections.

For every installed row DevClean retains:

- exact package ID;
- installed version;
- description;
- sdkmanager-reported `Location`;
- resolved local path when the Location is a strict descendant of the SDK root;
- measured current directory/file size.

A package row whose location cannot be confined to the exact SDK root is report-only.

The UI's measured size is a disk-space indicator, not an auto-cleanup signal.

## Why every package is USER_REVIEW

The Android SDK Manager exposes package uninstall as a supported operation, but the package manager cannot know which versions the user's local projects, CI scripts, AVDs, or offline workflows still require.

Examples:

- a project may compile against a specific installed platform;
- older projects may require a specific Build Tools or NDK revision;
- an AVD can depend on a particular system image;
- `platform-tools` provides ADB and related tooling;
- CMake/NDK packages can be pinned from Gradle build configuration.

These are known tradeoffs, not classification ambiguity. DevClean therefore never checks boxes automatically based on age or size and never sends the package list to AI by default.

## Self-hosting command-line tools are protected

The `sdkmanager` process is itself delivered inside Android command-line tooling. DevClean does not let the executing package manager remove the package that hosts the package manager.

Accordingly these package IDs remain non-executable in this lane:

- `cmdline-tools;*`;
- legacy `tools`.

The user can update/remove command-line tooling through Android Studio or another explicit administrative workflow, but DevClean will not attempt a self-uninstall that could disappear mid-operation.

## Local ownership boundary

An SDK root or package location can be redirected or placed on shared/removable storage. A valid package ID does not prove that the current DevClean instance exclusively owns that SDK.

Direct uninstall is enabled only when:

- the exact SDK root remains on local fixed storage without reparse redirection;
- the package's sdkmanager-reported Location resolves to a strict descendant of that root and remains local fixed;
- the package is not protected command-line tooling.

Shared, remote, removable, or reparse-redirected SDKs can still be inventoried, but uninstall is disabled.

## In-use guard

Before mutation DevClean refuses while Android tooling is actively using the SDK. The existing Android SDK process guard covers Android Studio, sdkmanager, Gradle, and Java command lines associated with those tools. This lane additionally blocks common runtime consumers such as:

- `adb`;
- `emulator`;
- `qemu-system-*`.

This is deliberately conservative. Package uninstall is explicit maintenance, so users can close the active tooling and retry rather than relying on partial file-lock behavior.

## Execution contract

For every selected package DevClean performs the following sequence independently:

1. confirm the exact SDK root is still one of the source-backed Android SDK roots;
2. re-run that root's own `sdkmanager --list`;
3. require the exact package ID to still be installed;
4. require the installed version and reported Location to match what the user selected;
5. require root and package location to remain confined to local fixed storage;
6. reject command-line-tool self-uninstall targets;
7. refuse while Android Studio/Gradle/sdkmanager/ADB/Emulator activity is present;
8. re-run the installed-package inventory immediately before mutation;
9. invoke only `sdkmanager --uninstall <exact-package-id> --sdk_root=<exact-root>`;
10. list installed packages again and require the package ID to be absent before reporting success;
11. measure the package's reported location before/after when possible;
12. stop a multi-package operation on the first failure or stale package identity.

There is no raw recursive-delete fallback.

## Output-language boundary

`sdkmanager --list` is a human-readable vendor interface, not a JSON API. DevClean forces the JVM/process locale to English/C for this invocation and requires the documented `Installed packages` table shape. If that table cannot be recognized, DevClean fails closed and grants no uninstall authority.

This avoids guessing package identity from directory structure when the vendor output changes.

## Explicit non-targets

This lane does not delete or modify:

- the complete Android SDK root;
- arbitrary `platforms`, `build-tools`, `system-images`, `ndk`, or `cmake` directories discovered by pathname alone;
- `cmdline-tools;*` or legacy `tools` through self-uninstall;
- Gradle caches;
- Android Studio caches/configuration;
- AVD user data;
- project `.gradle` or build directories;
- packages in remote/shared SDK installations.

## Primary sources

- Android Developers, **sdkmanager**: official package listing, install/update/uninstall syntax, SDK-style package IDs, and `--sdk_root`.
  - https://developer.android.com/tools/sdkmanager
- Android Developers, **Environment variables**: Android SDK location variables and SDK-root configuration context.
  - https://developer.android.com/tools/variables

## Product consequence

This closes the major Android SDK gap left by the earlier broad-root audit: the SDK root itself remains protected, but users can now reclaim large obsolete components one package at a time through the vendor package manager, with explicit intent and no filesystem guessing.
