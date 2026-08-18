# Conan 2 cache-clean audit

Audited: 2026-08-18

## Product conclusion

Conan 2 is a high-value deterministic cleanup target. Conan itself exposes a cache-cleaning command whose contract is specifically to remove non-critical generated folders. DevClean can therefore make this decision locally without AI and without guessing Conan's internal cache layout.

The supported action is:

```text
conan cache clean "*" -cc core:non_interactive=True
```

DevClean does **not** recursively delete `.conan2`, package folders, recipe exports, configuration, profiles, remotes, credentials, or any cache-internal path discovered by filename heuristics.

## Why this is safe enough for the deterministic lane

Current Conan 2 documentation describes `conan cache clean` as removing non-critical cache folders such as source, build and download storage. With the wildcard pattern and no narrowing flag, Conan removes the temporary, source, build and download folders that Conan generated for matching references.

The same documentation explicitly says Conan cache package storage must be considered read-only and callers should not modify, remove or add files directly. That is the key boundary for DevClean: Conan owns mutation of its cache; DevClean only invokes Conan's documented maintenance operation.

This differs from `conan remove`, which can remove recipes/packages themselves, and from `conan config clean`, which removes user configuration. Neither operation belongs in the universal safe-clean lane.

## Execution contract

Before mutation DevClean:

1. runs the selected Conan executable with `--version` and requires major version 2 or newer;
2. asks the same executable for the active home through `conan config home`;
3. requires the user-selected home to exactly match the vendor-reported absolute path;
4. refuses while Conan activity is detected;
5. inventories the home read-only and records before/after bytes;
6. invokes only `conan cache clean "*"` with `core:non_interactive=True`;
7. propagates any Conan error and never falls back to raw file deletion.

A total Conan home size of at least 1 GiB is used only as a benefit heuristic for default selection. The displayed home size includes persistent package artifacts, so it is not presented as an estimate of reclaimable bytes. Actual reclaimed bytes are measured after Conan completes.

## Version boundary

This implementation intentionally rejects Conan 1.x. Conan 2 changed cache architecture and provides the audited `conan cache clean` interface used here. DevClean must not silently reinterpret a Conan 1 cache through Conan 2 assumptions.

## Sources

- Conan 2.31 documentation, **conan cache**: `conan cache clean` removes non-critical source/build/download/temp cache folders and supports selection flags; the same page says package storage in the Conan cache must be treated as read-only and not modified directly.
- Conan 2.31 documentation, **conan config** / Conan examples: `conan config home` is the supported way to obtain the active Conan home.
- Conan 2.31 command reference: `conan remove` is a separate package/recipe removal operation and is not used by this maintenance path.
