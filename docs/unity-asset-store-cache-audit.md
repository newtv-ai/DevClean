# Unity Asset Store package-cache audit

Audited: 2026-08-18

## Product conclusion

Unity's Asset Store `.unitypackage` cache is a separate semantic source from project `Library` and from the Package Manager global cache used for registry/UPM packages.

Unity 6 explicitly documents a supported manual workflow for deleting one cached Asset Store `.unitypackage` file. It also states that deleting that cached copy does not remove assets that were already imported into projects.

That means DevClean does not need AI to decide what the object is. However, the local copy can still have personal value for offline re-import, slow networks, or assets that later become unavailable from the store. Therefore every package is **USER_REVIEW**:

- never selected by default;
- never sent to AI by default;
- removed only after explicit user selection and confirmation;
- never upgraded to whole-cache deletion merely because the cache is large.

## Source-backed storage boundary

Current Unity 6 documentation describes the Windows default Asset Store package-cache directory as:

`%APPDATA%\Unity\Asset Store-5.x`

The cache contains publisher-defined subdirectories and `.unitypackage` files.

Unity also supports changing the cache location through Preferences or with the `ASSETSTORE_CACHE_PATH` environment variable. Its documented cache structure remains:

`<asset-store-cache-root>\Asset Store-5.x\<publisher-defined subdirectories>`

Changing the location does not migrate or delete packages already stored in the previous location. Old packages can remain at the original root.

DevClean therefore inventories the documented default root even when an environment override exists, and also inventories the current `ASSETSTORE_CACHE_PATH` root when observable.

The Preferences UI stores a persistent setting, but the public Unity manual does not specify a stable external configuration key that DevClean can safely parse. DevClean deliberately does not depend on an undocumented EditorPrefs key. A user who changed the location in Preferences can add the location explicitly for the current maintenance session.

## Why whole-root deletion is rejected

Unity's documented removal procedure is package-specific:

1. identify the publisher and package display name;
2. locate the corresponding `.unitypackage` in the Asset Store cache;
3. delete that file.

The official documentation does not grant an equivalent "delete the whole Asset Store cache" contract.

A whole-root operation would also erase the user's entire offline package library in one action. DevClean therefore grants no recursive directory-delete authority to `Asset Store-5.x`, publisher folders, or product folders.

Only ordinary files with the `.unitypackage` extension can enter this maintenance action.

## Execution contract

Before deleting one selected package DevClean:

1. requires an exact cache root whose final directory name is `Asset Store-5.x`;
2. requires the selected file to be a strict descendant of that root;
3. requires the selected file to have the `.unitypackage` extension;
4. rejects cache-root symlinks/junctions and package-file links;
5. captures stable Windows identities for both the exact cache root and selected package;
6. rejects hard-linked package files;
7. executes through DevClean's handle-bound exact-file purge under the verified cache-root boundary;
8. requires the original selected path to be absent before reporting success;
9. never deletes siblings, publisher directories, product directories, project `Assets`, or UPM global-cache entries;
10. re-inventories after mutation and reports measured reclaimed bytes.

No raw recursive-delete fallback exists.

## Review-lane rationale

The distinction is:

- **Technical identity:** deterministic. Unity documents these files as cached Asset Store packages and documents individual deletion.
- **Personal value:** not deterministic. A cached copy may save a future download or preserve offline access.

This is exactly the DevClean `USER_REVIEW` lane: the product can explain the tradeoff cheaply and locally, so AI adds no value, but DevClean must not choose for the user.

## Explicit non-targets

This action does not delete or edit:

- project `Assets` or imported asset files;
- project `Library`;
- UPM/registry package global cache;
- Package Manager configuration;
- publisher/product directories as whole trees;
- files in the Asset Store cache that are not `.unitypackage` files;
- Unity Hub downloads, Editor installs, modules, templates, or projects.

## Primary sources

- Unity 6 Manual, **Delete a package from the Asset Store cache**: documents deleting an individual `.unitypackage`, states that imported project assets remain, and states that there is no equivalent procedure for removing UPM packages from the global cache.
  - https://docs.unity3d.com/cn/current/Manual/upm-del-pkg-as-cache.html
- Unity 6 Manual, **Asset Store packages**: documents the Windows default `Asset Store-5.x` location and publisher-defined subdirectory structure.
  - https://docs.unity3d.com/cn/6000.0/Manual/AssetStorePackages.html
- Unity 6 Manual, **Customize the Asset Store package cache location**: documents the separate cache, the `Asset Store-5.x` structure, Preferences override, `ASSETSTORE_CACHE_PATH`, and the fact that packages at the old location remain after changing cache location.
  - https://docs.unity3d.com/cn/current/Manual/upm-config-cache-as.html
