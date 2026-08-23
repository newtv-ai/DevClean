# npm cache maintenance audit

Audited/implemented: 2026-08-20  
Diagnostic-log boundary re-audited: 2026-08-23

## Product conclusion

Current npm exposes stronger cache lifecycle operations than DevClean's older raw subtree rules. DevClean therefore moves package/npx/TUF cache internals behind npm's own maintenance boundary.

Current lanes:

- package content cache (`<cache>/_cacache`) integrity + garbage collection via `npm cache verify`: **DETERMINISTIC_CANDIDATE / vendor GC**;
- whole package content cache clear via `npm cache clean --force`: **USER_REVIEW**;
- one exact full key returned by `npm cache npx ls`, removed through `npm cache npx rm <full-key>` after matching vendor dry-run: **USER_REVIEW**;
- TUF cache (`<cache>/_tuf`): **REPORT_ONLY / vendor-managed**;
- npm diagnostic logs: **USER_REVIEW**, with no generic whole-tree TOOL authority;
- global prefix, `.npmrc`, project/package metadata and unclassified cache-root state: protected.

The old generic raw-delete authority for `_cacache`, `_npx`, and `_tuf` remains removed. The 2026-08-23 diagnostic re-audit additionally removes the older age/size-based generic TOOL authority for `_logs`, configured `logs-dir` debug logs, and arbitrary `npm-debug.log` filenames.

See `docs/npm-diagnostic-log-authority-reaudit.md` for the focused logging evidence.

## Primary source

The cache-maintenance implementation was audited against npm CLI commit:

`51c2bf81fa2c31547d0fec44fff2aaac3d9a9862`

Primary files:

- `docs/lib/content/commands/npm-cache.md`
- `lib/commands/cache.js`
- `workspaces/config/lib/definitions/definitions.js`
- `test/lib/commands/cache.js`

Source URLs:

- https://github.com/npm/cli/blob/51c2bf81fa2c31547d0fec44fff2aaac3d9a9862/docs/lib/content/commands/npm-cache.md
- https://github.com/npm/cli/blob/51c2bf81fa2c31547d0fec44fff2aaac3d9a9862/lib/commands/cache.js
- https://github.com/npm/cli/blob/51c2bf81fa2c31547d0fec44fff2aaac3d9a9862/workspaces/config/lib/definitions/definitions.js
- https://github.com/npm/cli/blob/51c2bf81fa2c31547d0fec44fff2aaac3d9a9862/test/lib/commands/cache.js

Current npm documentation rechecked for the diagnostic boundary:

- https://docs.npmjs.com/using-npm/config/
- https://docs.npmjs.com/cli/v11/using-npm/logging/
- https://docs.npmjs.com/generating-and-locating-npm-debug.log-files/

## Exact cache-root semantics

npm's `cache` config is the **base cache root**. Current config flattening derives:

- package content cache: `<base>/_cacache`;
- npx cache: `<base>/_npx`;
- TUF cache: `<base>/_tuf`.

On Windows the default base is `%LocalAppData%\npm-cache`, but npm configuration/environment can redirect it.

DevClean therefore does not guess the active root from the default path. It resolves one exact npm executable, asks that executable for `npm config get cache`, then pins every later command with `NPM_CONFIG_CACHE=<exact-base-root>` and requires the same executable to confirm the pinned value.

The npm executable and an existing cache root are bound to ordinary local-fixed filesystem identities. Symlink/junction/reparse/cloud-placeholder roots are not granted mutation authority.

## Package content cache: why `verify` is deterministic vendor GC

npm's documentation describes the cache as self-healing and says `npm cache verify` verifies the cache contents, garbage-collects unneeded data, and verifies integrity.

Current `cache.js` delegates this to `cacache.verify(cachePath)`, where `cachePath` is npm's derived `_cacache` path. npm itself decides which content is unneeded and performs the GC/integrity maintenance.

DevClean therefore exposes **npm cache verify** as the preferred maintenance action rather than inventing an age/LRU policy or deleting `_cacache` internals.

Before the command DevClean:

1. re-discovers the current npm cache root through the same exact executable;
2. requires the cache-root and npm executable identities to remain unchanged;
3. refuses while npm/npx activity is visible or cannot be safely excluded;
4. fixes `NPM_CONFIG_CACHE` to the reviewed base root;
5. runs exactly `npm cache verify`;
6. re-inventories the same root afterward and reports logical before/after evidence.

No direct `content-v2`, `index-v5`, temp, integrity or cacache-file unlink is performed by DevClean.

## Whole package cache clear: USER_REVIEW

Current npm requires `--force` when `npm cache clean` is asked to clear the complete package cache. Source confirms that the command removes npm's derived `flatOptions.cache`, which is `<base>/_cacache`, not the entire base cache root.

Clearing `_cacache` does not uninstall already installed packages, but future operations may need to download package data again. The cache may also be intentionally valuable for offline or slow-network work.

DevClean therefore treats full clear as **USER_REVIEW**, not an automatic candidate.

The UI shows current npm-listed cache-key count and logical size. Before execution DevClean fresh-inventories and requires the reviewed package cache key set, file count and logical byte count to remain unchanged. It then runs only:

`npm cache clean --force`

with the exact pinned base root. Afterward DevClean re-inventories and requires the package cache to contain no vendor-listed content keys and zero logical content bytes before reporting success.

The presence of `--force` here does not mean DevClean accepts arbitrary forceful operations. It is the vendor-required flag for this one reviewed whole-package-cache command whose source-backed scope is `_cacache`.

## npx cache: exact vendor object USER_REVIEW

Current npm exposes:

- `npm cache npx ls`;
- `npm cache npx info <key>`;
- `npm cache npx rm <key>`;
- `npm cache npx rm <key> --dry-run`.

Upstream tests demonstrate that keys are not guaranteed to be hexadecimal. DevClean therefore does not invent a hash regex. It accepts only a **full simple basename exactly returned by vendor `npx ls`** and rejects path separators, `.`/`..`, NUL, duplicate keys, or malformed output.

npm itself supports abbreviated keys when resolving an npx removal target. DevClean deliberately does not use that feature: only the full exact vendor-returned key is passed back.

Before actual removal DevClean:

1. fresh-inventories the exact cache root;
2. requires the reviewed full key, exact path, logical bytes and file count to remain unchanged;
3. refuses while npm/npx is active;
4. runs `npm cache npx rm <full-key> --dry-run`;
5. parses npm's reported `Removing npx key at <path>` line and requires that path to equal exactly `<base>/_npx/<full-key>`;
6. fresh-inventories again and repeats the exact dry-run/path equality check;
7. runs `npm cache npx rm <full-key>` with **no force**;
8. re-inventories and requires the exact full key to be absent.

Removing all npx entries would require npm's force path; DevClean intentionally exposes no “remove all npx” action. Cached exec environments can avoid reinstall/download and therefore remain USER_REVIEW per exact entry.

## Why raw cacache-key removal is excluded

Current npm exposes `npm cache rm <key>`, but source comments explicitly note that deleting content by integrity can leave other cache entries without content. The cache is a content-addressable structure with shared relationships; a visible key is not a safe independent physical-object boundary for DevClean.

Accordingly, DevClean does not expose arbitrary content-key deletion. It uses vendor `verify` for GC and, only when the user explicitly wants the entire package cache gone, vendor full `clean --force`.

## TUF cache remains protected

npm derives `_tuf` from the same configured base cache, but the audited cache command does not expose an equally narrow object-level TUF cleanup action that DevClean can bind independently.

The UI reports its logical size for transparency but provides no raw delete button. Generic scanner authority is also removed from `_tuf`.

## Diagnostic logs: USER_REVIEW after the second pass

The earlier cache-maintenance audit intentionally left npm's diagnostic-log lane unchanged. A separate 2026-08-23 source review found that the old generic rule had over-interpreted npm's logging lifecycle.

Current npm defines the default log directory as `<cache>/_logs`, allows `logs-dir` to redirect it, and manages retention through `logs-max` (default 10): once the number of log files exceeds the configured maximum, npm removes the oldest logs. npm also documents these files as debug/troubleshooting records and warns that best-effort credential redaction should not be treated as a complete secrecy guarantee.

That contract does not justify DevClean's former `7 days + minimum bytes` automatic deletion rule.

Current generic scan semantics are therefore:

- default `<cache>/_logs`: **USER_REVIEW**;
- npm-shaped timestamped debug logs in an exact configured `logs-dir`: **USER_REVIEW**;
- unrelated siblings in a configured `logs-dir`: **KEEP**;
- legacy arbitrary-path `npm-debug.log`: recognizable **USER_REVIEW**, but the filename alone creates no directory or automatic-delete authority;
- no npm diagnostic log root is a deterministic whole-tree raw-delete root;
- user-approved mutation still requires npm/npx to be idle.

Package/npx/TUF vendor maintenance remains separate from this diagnostic lane. See `docs/npm-diagnostic-log-authority-reaudit.md`.

## Accounting

DevClean measures logical bytes without following symlink/junction boundaries for user explanation and before/after evidence. Logical bytes are not promised as equal Windows physical free-space recovery.

Vendor cache key counts are object/state evidence, not physical byte accounting.

## Deliberate exclusions

No authority is granted to:

- raw-delete the configured npm base cache root;
- raw-delete `_cacache`, `_npx`, `_tuf`, or `_logs` as generic TOOL directories;
- remove arbitrary cacache integrity/key entries;
- use abbreviated npx keys;
- run `npm cache npx rm` with no key / force-all;
- delete TUF state by directory name;
- automatically delete diagnostic logs by age/size or by a bare `npm-debug.log` filename;
- touch npm global installs, shims, `.npmrc`, package manifests or lockfiles;
- clean while npm/npx is active;
- infer delete authority from age, size, suffix or “redownloadable” status;
- claim logical-byte changes as guaranteed physical reclaim.

## Validation

Normal DevClean final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
