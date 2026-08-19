# Android SDK exact package maintenance and AVD system-image correlation

Last updated: 2026-08-19

## Product conclusion

The Android SDK root is installed developer tooling, not a cache. DevClean must keep the complete SDK tree protected.

A narrower vendor-owned lifecycle is available: **one exact installed package freshly reported by that SDK's own `sdkmanager` may be USER_REVIEW and uninstalled through `sdkmanager --uninstall <package-id> --sdk_root=<exact-root>`**.

This includes platforms, Build Tools, NDKs, CMake packages, emulator/system images, sources and other installed SDK packages. Rebuildability/downloadability does not make any of them automatically disposable: existing projects, CI configurations, old branches, offline workflows or virtual devices may still need a specific version.

System images receive an additional safety gate: DevClean correlates the exact installed package location with every source-backed AVD `config.ini` `image.sysdir.1` / `image.sysdir.2` reference it can prove. A referenced system image is protected. If the AVD reference proof is incomplete, **all system-image package uninstall authority fails closed**.

## Primary Android contracts

Current Android documentation establishes:

- `sdkmanager` lists installed/available SDK packages using SDK-style package paths such as `platforms;android-36`, `build-tools;36.0.0`, and `system-images;...`;
- `sdkmanager --uninstall <packages>` is the supported command-line uninstall lifecycle;
- an AVD contains a selected system image plus separate mutable storage/user data;
- the emulator's AVD system directory contains the read-only system image shared by AVDs of the same API/variant/architecture;
- AVD `config.ini` uses `image.sysdir.1` (and historically `image.sysdir.2`) for system-image search paths;
- AOSP emulator/SDK source defines those system-directory paths as either absolute or relative to the SDK installation root;
- Android Studio Device Manager treats system-image selection and AVD deletion/Wipe Data as separate lifecycles.

Primary sources:

- https://developer.android.com/tools/sdkmanager
- https://developer.android.com/studio/run/managing-avds
- https://developer.android.com/studio/run/emulator-commandline
- https://android.googlesource.com/platform/tools/base/
- https://android.googlesource.com/platform/external/qemu/

## Why the old generic SDK rules remain protected

The existing application scanner deliberately protects `system-images`, `platforms`, `build-tools`, NDKs, emulator binaries and unknown SDK children as installed payload. This package lane does **not** replace that policy with directory deletion.

Authority comes only from all of the following:

1. one source-backed Android SDK root already recognized by DevClean;
2. an ordinary non-reparse local-fixed SDK root with stable filesystem identity;
3. that SDK's own ordinary non-reparse local-fixed `sdkmanager` located under its `cmdline-tools`/legacy tools tree;
4. one exact package ID/version/Description/Location freshly listed in the `Installed packages` table;
5. the vendor-reported Location resolving to one ordinary existing non-reparse object strictly beneath the exact SDK root;
6. fresh revalidation of SDK root, sdkmanager identity, package identity and AVD reference state before mutation.

No directory name, age, size, regex guess or AI decision can substitute for that package identity.

## AVD system-image correlation

Android Emulator source defines `image.sysdir.1` and `image.sysdir.2` as the locations where an AVD looks for system images. Source also documents that these paths can be absolute or relative to the SDK installation root.

DevClean therefore reads only source-backed AVD content roots already discovered by the existing AVD audit and:

- requires each participating AVD content root and `config.ini` to be ordinary non-reparse objects;
- reads `config.ini` without mutation and requires file identity to remain stable around the read;
- fails closed on unreadable files, duplicate keys, missing `image.sysdir.1/2`, or otherwise unresolvable system-directory information;
- resolves an absolute system directory directly;
- resolves a relative system directory against every source-backed SDK root and conservatively treats every exact matching installed system-image package as referenced;
- never edits an AVD, runs Wipe Data, deletes AVD user data or snapshots, or assumes an old AVD can be discarded.

This deliberately overprotects when more than one SDK root contains the same relative system-image path. The emulator's effective SDK root can depend on how it is launched; protecting every plausible exact match is safer than guessing which SDK a future launch will use.

### Incomplete AVD proof

If even one discovered AVD cannot be safely correlated, non-system-image packages may still retain their independent sdkmanager USER_REVIEW lifecycle, but **every `system-images;...` package becomes non-executable**. An unresolved AVD could be the only consumer of the selected image.

### Unreferenced is still USER_REVIEW

A system image with zero current static AVD references is not a deterministic cleanup candidate. Users may intentionally keep images for future AVD creation, offline work, testing old Android versions or avoiding large re-downloads.

## sdkmanager self-host protection

`cmdline-tools;*` and legacy `tools` are protected from this lane. They host the sdkmanager execution surface itself. DevClean does not allow a maintenance command to remove the tool package that owns the operation while it is executing.

## Process/concurrency boundary

Before uninstall DevClean fails closed while relevant Android tooling is active, including:

- Android Studio;
- Gradle/sdkmanager Java entry points already covered by the SDK writer guard;
- ADB;
- Android Emulator/QEMU processes.

The process state is checked before expensive fresh inventory and again immediately before mutation. Uncertain process-query state fails closed through the existing guards.

## Mutation

The only mutation command is:

```text
<exact-sdkmanager> --uninstall <exact-package-id> --sdk_root=<exact-sdk-root>
```

DevClean never:

- recursively deletes the package Location itself;
- deletes the complete SDK root;
- supplies multiple package IDs in one user action;
- uninstalls a system image referenced by an AVD;
- uninstalls system images when AVD proof is incomplete;
- uninstalls `cmdline-tools;*` or legacy `tools`;
- passes user- or AI-generated extra sdkmanager arguments.

After sdkmanager succeeds, DevClean lists installed packages again and requires the exact package ID to be absent. Package-directory before/after size is observational evidence only and is not used as identity or deletion authority.

## Stable identity and storage boundary

The SDK root, sdkmanager and exact vendor Location must remain ordinary non-reparse objects. Root/sdkmanager file identities and the package Location identity are retained across review and revalidation. A Location that escapes the SDK root, redirects through a symlink/junction/reparse path, disappears unexpectedly, or lands on unapproved storage is report-only.

This is stricter than merely trusting the text in sdkmanager's Location column because DevClean is a local-disk cleanup product and must not accidentally operate through redirected/shared storage.

## Decision class

Every executable package is **USER_REVIEW**. Nothing in this lane is auto-selected and nothing needs AI classification.

The UI should emphasize:

- exact package ID/version/Location;
- measured current package-directory size as benefit context only;
- SDK root;
- for system images, exact AVD names that reference the package or the reason reference proof is incomplete;
- why protected packages cannot be executed.

## Deliberate exclusions

This audit grants no authority to:

- raw SDK package-directory deletion;
- whole `system-images`, `platforms`, `build-tools`, `ndk`, `cmake`, `emulator`, `sources` or SDK-root deletion;
- AVD deletion, Wipe Data, snapshots or user-data mutation;
- project dependency guessing from directory scans;
- automatic removal of old package versions;
- AI-created sdkmanager commands.

Project/build-file correlation for platforms/Build Tools/NDK/CMake could improve explanations later, but missing project discovery must not be interpreted as "unused".
