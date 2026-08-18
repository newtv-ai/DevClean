# Unity Global Illumination (GI) cache audit

Audited: 2026-08-18

## Product conclusion

Unity's `GiCache` is a real generated cache, but it is **not** a DevClean raw-delete target.

Current Unity 6 documentation gives the GI cache its own lifecycle controls:

- the cache is shared by all Unity projects on the machine;
- its maximum size is configurable in Unity Preferences;
- Unity periodically removes unused entries, oldest first, to stay near that limit;
- this pruning is automatic and normally requires no user action;
- Unity exposes a **Clean Cache** action that releases Editor references before clearing;
- Unity separately warns that clearing the entire GI cache should be a **last resort**.

DevClean therefore classifies the source semantically as **Unity-managed / protected**. AI adds no value, and disk usage alone must never promote it to generic recursive deletion.

## Why a generated cache is still protected

Being rebuildable is only one part of a cleanup decision.

Unity documents the GI cache as intermediate lighting data used to accelerate subsequent baked or real-time global-illumination precomputation. The cache is shared across projects with compatible content/lightmapping backends, and Unity even documents copying `GiCache` between machines to avoid recomputation.

Deleting it can therefore destroy useful cross-project and cross-machine acceleration state. More importantly, Unity already has application-aware lifecycle logic that knows which entries are unused and removes the oldest entries automatically when needed.

The correct DevClean rule is not "cache => safe to delete". It is:

**known vendor-managed cache with its own GC => keep visible if a future exact inventory source exists, but do not invent a competing raw-deletion policy.**

## Why `Clean Cache` is not automated by DevClean

Unity's Preferences documentation exposes a **Clean Cache** button and explains why manual filesystem deletion is unsafe while the Editor is running: the Editor keeps references/hashes for GI cache files, and unexpected disappearance can leave the subsystem unable to recover correctly. The vendor action releases references before removing files.

However, the current public Unity documentation does not expose that Preferences action as a stable headless command, commandlet, CLI, or documented external API that DevClean can safely invoke.

DevClean therefore does not:

- simulate the Preferences button with raw filesystem calls;
- terminate Unity and delete `GiCache` itself;
- use undocumented Editor internals to trigger cache cleaning;
- silently map "Clean Cache" to a generic recursive delete.

If a future Unity version publishes a supported external cleanup command/API, this decision can be revisited as a vendor-managed action.

## Location boundary

Unity 6 lets the user change the GI cache location in Preferences and also documents custom placement through Unity Editor special command-line arguments.

The public GI-cache documentation does not provide a single stable external configuration file/key that DevClean can parse to prove the currently effective location for every Editor installation.

That matters because `GiCache` can also be copied between machines. A directory merely named `GiCache`, `Cache`, or `Caches` is not enough to establish ownership or deletion authority.

DevClean therefore deliberately adds **no hard-coded raw-delete root** for GI cache storage and does not try to discover it by directory-name guessing.

## Current Unity-owned lifecycle

The current Preferences documentation describes:

- **Maximum Cache Size (GB)**: Unity tries to keep the GI cache below this limit;
- automatic periodic deletion of unused cache files, oldest first;
- **Cache Folder Location**: user-configurable cache placement;
- **Cache Compression**: Unity-controlled tradeoff between disk footprint and decompression work;
- **Clean Cache**: vendor-controlled full-cache clearing.

The documentation explicitly says automatic deletion normally requires no user action. If all cache data is actively needed by the current scene, Unity recommends increasing the cache size rather than repeatedly forcing expensive recomputation.

This is precisely the kind of application-owned storage policy DevClean should respect rather than replace.

## Last-resort semantics

Unity 6's dedicated GI-cache documentation states that clearing the cache should be reserved as a last resort, and asks users to report projects/problems that force such clearing as bugs.

That is stronger than ordinary USER_REVIEW semantics. The vendor is not presenting full-cache clearing as routine disk housekeeping.

Accordingly DevClean does not put GI full-cache deletion in:

- deterministic/default cleanup;
- ordinary user-review cleanup;
- AI review.

It remains protected/vendor-managed.

## Explicit non-targets

This audit grants no deletion authority to:

- any directory merely named `GiCache`;
- Unity `Caches` parents;
- a user-selected/custom GI cache location;
- shared/copied GI cache data;
- project Lighting Data Assets;
- project `Library`;
- project `Assets`;
- Asset Store package cache;
- Package Manager global cache.

Those sources keep their own independent semantic decisions.

## Primary sources

- Unity 6 Manual, **Preferences — GI Cache**: shared cache semantics, configurable maximum size/location/compression, automatic oldest-unused-file pruning, and the vendor `Clean Cache` control.
  - https://docs.unity3d.com/kr/current/Manual/Preferences.html
- Unity 6 Manual, **Global Illumination (GI) cache**: intermediate lighting data, cross-project sharing/copying, custom location support, and the warning that clearing the GI cache should be a last resort.
  - https://docs.unity3d.com/cn/6000.0/Manual/GICache.html

## Follow-up condition

Re-open this audit only if Unity publishes a documented external cleanup/GC command or API whose target and lifecycle semantics DevClean can verify without relying on private Editor state. Until then, the absence of a GI cleanup button in DevClean is intentional safety behavior, not missing coverage.
