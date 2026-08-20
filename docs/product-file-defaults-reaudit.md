# Product file defaults / learned-rule portability re-audit

Audited: 2026-08-21

## Architecture

Raw `AI_IMPORT` and `USER_DECISION` history remains local. Reusable product knowledge uses a separate `PRODUCT_AUDITED` source. On every normal load, the current executable overlays only its product rules onto the active local sidecar rules; stale product copies in sidecars cannot pin an old product definition. `AI_IMPORT` still counts and clears separately.

All non-directory decision sources are consumed only through the file matcher. Directories use the separate `USER_DIRECTORY_DECISION` exact-path matcher, so a product glob such as an NVIDIA `*.bin` rule cannot authorize a directory whose name happens to end in `.bin`. Explicit kept directories continue to suppress descendant file deletes.

The legacy contamination migration now keys on the historical `AI_IMPORT` provenance instead of assuming packaged defaults are forever empty. This allows deliberately audited defaults without preserving the accidental development-machine snapshot that #142 removed.

## Promoted DELETE knowledge

The old development-machine rules repeatedly observed files under `%LOCALAPPDATA%\NVIDIA\DXCache` and `%LOCALAPPDATA%\NVIDIA\GLCache`. NVIDIA documents shader disk caches as compiled-shader acceleration, says driver changes cause recompilation, documents automatic cache eviction, and explicitly describes removing an old GLCache as safe because the driver repopulates a fresh cache. The product therefore promotes only the observed Windows file shapes (`DXCache\*.nvph`, `DXCache\*.bin`, `GLCache\*\*\*.bin`). It does **not** grant whole-directory authority.

Primary vendor references:

- https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-gb/mergedProjects/nv3dENG/Manage_3D_Settings_%28reference%29.htm
- https://download.nvidia.com/XFree86/Linux-x86_64/555.58/README/openglenvvariables.html

## Promoted KEEP knowledge

The old rules also correctly identified JetBrains-managed JDK `lib\modules` files under `%USERPROFILE%\.jdks`. Oracle documents the installed JDK `lib` tree as private runtime implementation details that must not be modified. The product converts that observation into a conservative file-only KEEP glob. This is protection, not a cleanup candidate.

Primary vendor reference:

- https://docs.oracle.com/en/java/javase/26/install/installed-directory-structure-jdk.html

## Deliberately not restored

The rest of the old machine snapshot is not copied back merely because an AI once labeled it. Major rejected classes include:

- Claude/Codex transcripts and current-version state: personal retention or current-use semantics;
- JDK source archives, PDBs, GraalVM profiles and other installed payload: installation/feature state, not cache authority;
- EasyOCR/model weights and browser local models: redownloadable does not mean low-cost or user-independent;
- Android AVD snapshots: exact emulator lifecycle and user boot-performance preference belong to the Android-specific lane;
- browser component/Safe Browsing caches, VSIX caches, npm cache and IDE indexes already covered by stronger application/vendor rules: duplicating them as generic learned globs would weaken the source-owned boundary;
- Tencent/QQ dynamic packages and other third-party application resources: no current primary-source lifecycle proof was established in this pass;
- MySQL/InnoDB files, SQLite WAL and similar persistent state: protection belongs to semantic hard guards/application state, not copied machine paths.

The promotion rule is therefore: a development-machine observation may seed research, but it becomes product knowledge only when the object type is common, file-scoped, source-supported, and no stronger source-specific rule already owns it.
