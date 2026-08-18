# Unity Package Manager global-cache audit

Audited: 2026-08-18

## Product conclusion

Unity Package Manager (UPM) global storage is not one disposable cache tree. Unity 6 gives its current registry cache its own size limit and least-recently-used eviction behavior, while a historical `packages` subtree has a different lifecycle and an optional Git LFS cache exists for download reuse.

DevClean therefore splits the global cache into three independent semantic sources:

| Storage | DevClean decision | Mutation |
| --- | --- | --- |
| active registry `db` | `UNITY_MANAGED` | none; Unity owns size/LRU GC |
| deprecated `packages` | `USER_REVIEW` | exact local-directory removal only after explicit user confirmation |
| `git-lfs` | `REPORT_ONLY` | none |

AI adds no value to any of these decisions. The unknown question for the deprecated `packages` directory is user intent about older Unity projects, not technical file identity.

## Current Unity 6 layout

Unity's current manual defines the Package Manager global cache separately from the Asset Store package cache. Under the global cache root it describes:

- `db`: registry package metadata and tarballs;
- `git-lfs`: downloaded Git LFS objects when that cache is enabled;
- `packages`: a deprecated historical subtree that can remain from projects created with Unity Editor 2023.2.

The documented Windows default global-cache root is under the user's local Unity cache:

`%LOCALAPPDATA%\Unity\cache\upm`

Unity 6 no longer uses the historical `packages` subtree. Unity's upgrade guide says that users who no longer maintain projects requiring the older Editor can safely remove it, and that removal is optional.

This is a narrow conditional contract. It does **not** grant whole-root deletion authority to `%LOCALAPPDATA%\Unity\cache\upm`.

## Why the active `db` is Unity-managed instead of a DevClean cleanup target

Starting with Unity 2023.2.0f1, the registry data cache has a default maximum size of 10 GB. Package Manager trims the cache when necessary using least-recently-used package installation activity.

Unity also exposes a user-configurable maximum (`maxCacheSize`) and an environment override (`UPM_MAX_CACHE_SIZE`). Reducing the maximum causes Package Manager to remove cached content until it falls under the configured size.

That means current Unity already owns the authoritative garbage-collection semantics for this store. DevClean can inventory the active `db` and explain its configured limit, but it must not compete with Unity using raw filesystem deletion, age guesses, filename guesses, or a second independent LRU implementation.

A very large `db` therefore remains **visible but non-deletable** in DevClean. Size is evidence for the user, not authority for DevClean.

## Configuration and location precedence

Unity 6 supports changing the global cache with Package Manager Preferences, user configuration, and environment variables.

The user configuration file can define:

- `cacheRoot`: the global cache root;
- `maxCacheSize`: the registry `db` maximum in bytes.

On Windows the normal user configuration file is `%USERPROFILE%\.upmconfig.toml`; `UPM_USER_CONFIG_FILE` can point to another user configuration file.

Relevant environment variables include:

- `UPM_CACHE_ROOT`: override the global cache root;
- `UPM_NPM_CACHE_PATH`: override the registry `db` location specifically;
- `UPM_GIT_LFS_CACHE_PATH`: override the Git LFS cache location and enable it;
- `UPM_ENABLE_GIT_LFS_CACHE`: enable the Git LFS cache at its normal location;
- `UPM_MAX_CACHE_SIZE`: override the `db` maximum size.

Environment-variable values take precedence over the user configuration for the corresponding settings.

Unity also documents that changing a cache location does not migrate or delete data at the previous location. DevClean therefore keeps lower-priority/default roots visible when they still exist instead of pretending that the current effective root is the only disk consumer.

Unity explicitly lists shared drives as one reason a user may relocate the cache. That matters to DevClean: a source-backed UPM path can still be shared state. Shared, remote, removable, or reparse-redirected roots remain visible, but DevClean does not offer destructive maintenance for them.

Unity 6's upgrade guide explicitly says the old `UPM_CACHE_PATH` variable is no longer supported; DevClean does not revive or interpret that obsolete variable as deletion authority.

## Deprecated `packages`: USER_REVIEW

The old `packages` subtree is the only current UPM global-cache component in this audit with a direct deletion path.

The source-backed condition is precise:

- Unity 6 no longer uses it;
- it can remain from Unity 2023.2-era projects;
- Unity says it may be safely removed if the user no longer maintains projects that require the older Editor;
- removal is optional.

DevClean therefore classifies an exact `<audited-cache-root>\packages` directory as `USER_REVIEW`:

- never selected by default;
- never sent to AI by default;
- always requires an explicit confirmation that the user no longer needs the older-Editor workflow;
- only becomes executable on a local fixed volume with no reparse redirection in its boundary;
- remains report-only for execution purposes when the cache is shared/remote/removable;
- never authorizes a sibling `db`, `git-lfs`, or the cache root itself.

## Git LFS cache: report only

Unity documents the Git LFS cache as an optional store of downloaded Git Large File Storage objects. Keeping the objects can avoid downloading them again.

This is a cache, but its value depends on network/offline conditions and the packages/projects the user continues to use. More importantly, the current public documentation does not expose a stable per-object or garbage-collection action that DevClean can safely delegate to.

DevClean therefore inventories the exact effective Git LFS cache, including a custom `UPM_GIT_LFS_CACHE_PATH`, but grants no raw-delete or whole-tree authority.

## Execution contract for legacy `packages`

Immediately before a destructive action DevClean:

1. re-parses the current supported UPM configuration and environment overrides;
2. requires the selected cache root to still exactly match one of the source-backed current/historical roots DevClean can confirm;
3. grants authority only to its direct `packages` child;
4. requires both root and target to be ordinary directories, not symlinks or Windows junctions;
5. requires both boundary and target to remain on a local fixed volume and rejects shared/remote/removable/reparse-redirected storage;
6. refuses while Unity Editor, Unity Hub, or Unity Package Manager activity is present;
7. captures stable Windows identities for both the cache-root boundary and exact `packages` directory;
8. uses DevClean's handle-bound exact-directory purge, never `rmtree` or a generic recursive-delete fallback;
9. never descends through reparse points;
10. requires the exact `packages` root to be absent before reporting success;
11. measures before/after/reclaimed bytes and re-inventories after the operation.

A directory named `packages` elsewhere receives no authority from this audit.

## Explicit non-targets

This lane does not delete or modify:

- the active registry `db`;
- an inactive/old `db` merely because it is large;
- `git-lfs` contents;
- the complete UPM global-cache root;
- shared/remote UPM cache contents;
- project `Library`;
- project `Packages`, `manifest.json`, or package lock state;
- Asset Store `.unitypackage` cache data;
- Unity Editor/Hub installations or modules;
- `.upmconfig.toml` itself or any Package Manager configuration.

## Primary sources

- Unity 6 Manual, **Global cache**: current global-cache purpose and `db` / `git-lfs` / deprecated `packages` layout, including the condition under which the historical `packages` subtree can be removed.
  - https://docs.unity3d.com/cn/current/Manual/upm-cache.html
- Unity 6 Manual, **Customize the global cache**: `cacheRoot`, `maxCacheSize`, Preferences, environment overrides, precedence, custom `db` / Git LFS paths, shared-drive use cases, and the current Package Manager cache-size behavior.
  - https://docs.unity3d.com/cn/current/Manual/upm-config-cache.html
- Unity 6 Manual, **Upgrade to Unity 6**: `packages` is no longer used by Package Manager, optional conditional deletion guidance, and removal of support for `UPM_CACHE_PATH`.
  - https://docs.unity3d.com/cn/current/Manual/UpgradeGuideUnity6.html
