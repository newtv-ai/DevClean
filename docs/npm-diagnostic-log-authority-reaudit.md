# npm diagnostic-log authority re-audit — 2026-08

## Why this row was reopened

The 2026-08 npm cache-maintenance audit correctly removed generic raw deletion
from `_cacache`, `_npx`, and `_tuf` and replaced it with npm-owned maintenance.
It deliberately left the older diagnostic-log lane unchanged.

That remaining lane still granted TOOL authority by DevClean-specific age/size
heuristics:

- default `<cache>/_logs`: recursive TOOL root after 7 days / 1 MiB;
- npm-shaped files in a configured `logs-dir`: TOOL after 7 days / 256 KiB;
- any file named `npm-debug.log`, anywhere: TOOL after 7 days / 256 KiB.

A fresh source review does not support those thresholds as npm lifecycle
semantics. This re-audit fixes only diagnostic-log authority; npm package/npx/TUF
vendor maintenance remains unchanged.

## Current npm logging contract

Current npm documentation defines:

- `logs-dir`: the directory where npm writes its diagnostic logs; the default is
  `_logs` inside the configured cache;
- `logs-max`: the maximum number of log files to store, default `10`;
- when the number of log files exceeds `logs-max`, npm removes the oldest logs;
- `logs-max=0` disables writing log files for the current run;
- failed npm commands print the debug-log path because the logs are intended for
  troubleshooting;
- `--timing` may also write timing information into the cache or configured
  `logs-dir`;
- npm makes a best effort to redact credentials but explicitly warns users not
  to rely on complete redaction of sensitive information.

Primary current references:

- https://docs.npmjs.com/using-npm/config/
- https://docs.npmjs.com/cli/v11/using-npm/logging/
- https://docs.npmjs.com/generating-and-locating-npm-debug.log-files/

The important boundary is that npm already has a **count-based diagnostic-log
retention policy**. It does not define a seven-day or minimum-byte expiration
contract that grants an external cleaner unconditional deletion authority.

## Default `<cache>/_logs`: USER_REVIEW

The exact default `_logs` path proves these are npm diagnostic records, not
unknown application state. But diagnostic history may still be useful after a
failed install/publish/exec command, and npm itself decides how many recent logs
to retain through `logs-max`.

Therefore the default log subtree is now:

- semantic owner: `USER`;
- action: `USER_DECISION`;
- no deterministic whole-tree TOOL authority;
- age/size/unknown-last-use affect explanation/ranking only;
- a user-approved mutation still requires npm/npx activity to be absent.

DevClean does not attempt to reproduce npm's `logs-max` lifecycle from mtimes.
If npm wants to retire older logs, npm already does so when its own log writer
runs.

## Configured `logs-dir`: exact npm-shaped files are USER_REVIEW

npm permits `logs-dir` to point outside the cache, including a location that may
also contain unrelated files. DevClean therefore keeps the existing narrow
filename pattern for npm's timestamped debug logs and leaves all unclassified
siblings protected.

The matched npm-shaped file is USER, not TOOL. The configured directory itself
never becomes a recursive delete root.

This matters because a configured log directory can be shared or deliberately
chosen for troubleshooting. Directory configuration proves product ownership of
matching npm log records; it does not prove every file in the parent directory
is disposable.

## Legacy `npm-debug.log`: filename alone cannot create TOOL authority

Older npm versions generated `npm-debug.log` files and npm documentation still
explains how such logs are used to diagnose failed commands. DevClean may
recognize the basename as a useful user-review hint, but the basename can occur
in an arbitrary project or user directory.

Accordingly:

- `npm-debug.log` remains recognizable;
- it is `USER_REVIEW`, not TOOL;
- no directory authority is inferred from the filename;
- freshness, size, or age cannot promote it to automatic deletion.

This follows the general DevClean rule that file-level knowledge must not be
generalized to directory-level authority.

## Package-cache maintenance remains unchanged

This re-audit does **not** weaken the source-backed npm maintenance work:

- `npm cache verify` on the exact configured `_cacache` remains deterministic
  vendor GC;
- full `_cacache` clear remains explicit USER_REVIEW through
  `npm cache clean --force`;
- exact vendor-listed npx entries remain USER_REVIEW through
  `npm cache npx rm <full-key>` after dry-run path proof;
- `_tuf` remains report-only/vendor-managed;
- no raw deletion authority is granted to `_cacache`, `_npx`, `_tuf`, or the
  configured npm base cache root.

See `docs/npm-cache-maintenance-audit.md`.

## Retained protection

No authority is added for:

- global npm packages or executable shims;
- `.npmrc`;
- `package.json`, `package-lock.json`, or `npm-shrinkwrap.json`;
- unclassified npm-cache-root state;
- unrelated files in a configured `logs-dir`;
- project `node_modules` merely because npm is installed.

## Validation requirements

Regression coverage must prove:

- default `_logs` -> USER_DECISION regardless age/size/last-use;
- configured npm debug-log files -> USER while unclassified siblings stay KEEP;
- legacy `npm-debug.log` -> USER and never creates parent-directory authority;
- npm/npx activity remains an execution blocker for user-approved log removal;
- no npm diagnostic log root is exposed as deterministic whole-tree
  VENDOR_MANAGED cleanup;
- package/npx/TUF raw-deletion protections remain unchanged.

The normal final-head lock/dependency, Ruff, strict mypy, full pytest, Windows
EXE, and CodeQL gates remain mandatory before merge.

After Visual Studio PR #190 merged into `main`, this branch received a fresh
synchronize commit so the final pull-request merge ref is revalidated against
the updated `main` rather than relying on the earlier pre-#190 green run.
