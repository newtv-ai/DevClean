# Yarn cache authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's former generic whole-tree delete authority for Yarn machine caches is too broad and is removed.

Current product lanes are:

- Yarn Classic machine cache discovered by `yarn cache dir`: **REPORT_ONLY / protected from generic raw deletion**;
- modern Yarn machine-global cache/mirror under `globalFolder/cache`: **REPORT_ONLY / protected from generic raw deletion**;
- project-local `.yarn/cache`: user/offline/Zero-Installs state and therefore protected from generic cleanup;
- modern `globalFolder` state/store and project `.yarn` metadata: protected;
- no generic Yarn whole-tree mutation lane.

This is a safety correction. “It is a package cache” proves rebuildability in many cases, but does not prove that DevClean can delete the previously exposed root with the same scope as the vendor's current lifecycle command.

## Primary sources

### Yarn Classic

Audited against `yarnpkg/yarn` commit:

`c2dda503f3759b5be5f0e24ecd9cf5c97a540147`

Primary files:

- `src/cli/commands/cache.js`
- `src/config.js`

Source URLs:

- https://github.com/yarnpkg/yarn/blob/c2dda503f3759b5be5f0e24ecd9cf5c97a540147/src/cli/commands/cache.js
- https://github.com/yarnpkg/yarn/blob/c2dda503f3759b5be5f0e24ecd9cf5c97a540147/src/config.js

### Modern Yarn / Berry

Audited against `yarnpkg/berry` commit:

`57081c05a398f25c92df1dc78752f2053576cec0`

Primary files:

- `packages/plugin-essentials/sources/commands/cache/clean.ts`
- `packages/yarnpkg-core/sources/Cache.ts`
- `packages/yarnpkg-core/sources/Configuration.ts`

Source URLs:

- https://github.com/yarnpkg/berry/blob/57081c05a398f25c92df1dc78752f2053576cec0/packages/plugin-essentials/sources/commands/cache/clean.ts
- https://github.com/yarnpkg/berry/blob/57081c05a398f25c92df1dc78752f2053576cec0/packages/yarnpkg-core/sources/Cache.ts
- https://github.com/yarnpkg/berry/blob/57081c05a398f25c92df1dc78752f2053576cec0/packages/yarnpkg-core/sources/Configuration.ts

## Yarn Classic: `cache dir` is not the destructive root

Yarn Classic's configuration distinguishes:

- `_cacheRootFolder`: configured/base cache root;
- `cacheFolder`: `<_cacheRootFolder>/v<CACHE_VERSION>`.

`yarn cache dir` reports `config.cacheFolder`.

However, `yarn cache clean` with no package arguments does **not** merely remove that reported version directory. Current Classic source calls `fs.unlink(config._cacheRootFolder)` and then recreates `config.cacheFolder`.

DevClean's previous generic lane granted whole-tree authority to the path returned by `yarn cache dir`. That was not the same lifecycle boundary as the vendor command. A narrower raw delete can still be unsafe as a product contract: it bypasses Yarn's cache-version lifecycle and assumes directory structure is the stable API.

Classic also supports `yarn cache clean <package-name>`, but this resolves cached package metadata by package **name** and can remove all matching cached entries/versions. It is not an exact one-object-by-version delete API suitable for silently converting scanner findings into independent deletion authority.

A future Classic executable lane may be possible as explicit **USER_REVIEW**, but it must bind one exact Classic CLI version, prove the effective base cache root rather than infer it from a generic folder name, pin configuration so the action cannot be redirected, and review the full whole-cache scope actually owned by `yarn cache clean`.

Until that dedicated lane exists, generic direct deletion is removed.

## Modern Yarn: project/configuration semantics matter

Modern Yarn defaults `cacheFolder` to project-local `./.yarn/cache`, while `enableGlobalCache` defaults true and internally redirects the effective cache folder to `<globalFolder>/cache`.

This means the effective cache location is a configuration result, not a universal filesystem convention.

Project-local `.yarn/cache` is especially sensitive because Yarn explicitly supports Zero-Installs/offline workflows. DevClean already protects that state from generic cleanup, and this audit retains that boundary.

## Why modern `yarn cache clean` is not a generic machine-cache button

Current modern Yarn source exposes:

- `yarn cache clean` / `cache clear` for the effective local cache;
- `--mirror` for the global mirror;
- `--all` for both.

The command first loads `Configuration.find(this.context.cwd, this.context.plugins)`, so its scope depends on the current project/configuration/plugin environment.

It also refuses to run unless `enableCacheClean` is already true and explicitly warns that cache cleaning should be avoided with Zero-Installs.

For mirror/global cleanup, the source does more than remove `cache.mirrorCwd`: after deleting the mirror it invokes the plugin hook `cleanGlobalArtifacts` for the active plugin set. Therefore `yarn cache clean --mirror` is **not** a guaranteed single-directory mutation contract.

DevClean will not:

- set `enableCacheClean: true` on the user's behalf;
- execute a project/plugin configuration merely to discover a destructive scope;
- treat `--mirror` as equivalent to deleting exactly `<globalFolder>/cache`;
- run `--all`;
- bypass the vendor's plugin lifecycle by retaining an age-based raw directory delete rule.

## Why age/size-based TOOL_DELETE was removed

The old DevClean profile could turn the Yarn machine cache into `TOOL_DELETE` after a fixed age/size threshold and then grant whole-tree generic deletion authority.

That fails the current product standard for two reasons:

1. age and size are benefit signals, not mutation authority;
2. the current Yarn vendor lifecycles are broader/configuration-sensitive in ways the generic root rule did not model.

The scanner can still identify and measure these locations for explanation. The decision is now KEEP/REPORT_ONLY until a dedicated exact lifecycle is implemented.

## Project-local and persistent state

No authority is granted to:

- `.yarn/cache` as a generic project cache;
- `.yarn/patches`, `plugins`, `releases`, `sdks`, `versions`, `unplugged`;
- `.yarn/install-state.gz` / build state;
- `yarn.lock`, `.yarnrc`, `.yarnrc.yml`, `.pnp.*`;
- modern global store/state outside the specifically identified cache path;
- offline mirrors configured separately by Yarn Classic.

Unknown future `.yarn` project children remain protected by default.

## Revisit conditions

A future executable Yarn lane must be separated by Yarn generation.

### Classic revisit

Require an exact Classic CLI identity/version and a source-backed method to prove the **base** cache root that the vendor whole-cache command will remove. The action should be explicit USER_REVIEW because deleting the whole package cache imposes redownload/offline cost. Fresh config/root/process revalidation and postconditions are required.

### Modern revisit

Require a non-project-executing, non-plugin-expanding vendor operation whose destructive manifest can be bounded to one exact cache/mirror root. Alternatively, a future Yarn API may expose a machine-readable exact cleanup plan that includes all hook side effects. DevClean must not modify `enableCacheClean` merely to force the command to run.

## Validation

This PR changes the executable boundary by removing generic whole-tree authority and adds regression tests that machine caches remain visible but report-only regardless of age or size. Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
