# npm protected mutation-scope audit — 2026-08

## Finding

The existing npm authority model remains correct:

- npm diagnostic logs are USER_REVIEW;
- package cache maintenance is delegated to npm's vendor commands;
- full `_cacache` clearing remains a user-approved vendor action;
- exact npx entries are removed only after npm lists the full key and two dry-run
  checks confirm the same exact entry path;
- `_tuf`, global installs, user configuration, and unrelated persistent state are
  not raw filesystem cleanup targets.

A separate execution-boundary gap was found while preparing a later generic-scan
performance audit.

Current npm CLI source derives three cache subpaths from the configured base
`cache` value:

- `flatOptions.cache = <base>/_cacache`;
- `flatOptions.npxCache = <base>/_npx`;
- `flatOptions.tufCache = <base>/_tuf`.

Current `npm cache clean --force` recursively removes `flatOptions.cache`, and
current `npm cache npx rm <key>` recursively removes the selected exact npx entry
path. These are valid vendor operations for normal npm layouts, but npm also
allows other configuration paths to be redirected independently.

Before this correction, DevClean revalidated the bound npm CLI, configured cache
root, cache inventory, and exact npx entry, but it did not prove immediately
before mutation that persistent/user paths such as `prefix`, `userconfig`, or
`logs-dir` had not been redirected inside the vendor command's recursive mutation
range. In such a pathological but valid configuration, invoking the vendor
command would be broader than the authority DevClean presented to the user.

This is an execution-scope bug, not a classification bug and not new cleanup
authority.

## Correction

Immediately before each npm content-cache mutation, DevClean now asks the same
bound npm executable for the effective values of:

- `cache`;
- `prefix`;
- `userconfig`;
- `logs-dir`.

The query runs under the already-pinned `NPM_CONFIG_CACHE` environment.
DevClean fails closed unless all required keys are returned and npm again confirms
the same reviewed base cache root.

For each mutation root, DevClean then rejects execution if any protected path is
inside that root:

- npm global `prefix`;
- npm `userconfig`;
- explicitly configured `logs-dir` when non-null;
- the normal `<cache>/_logs` diagnostic-log location.

The containment check includes normalized and non-strict resolved forms so normal
Windows path spelling/case differences do not bypass the boundary check. No
filesystem deletion fallback is introduced.

## Operation-specific boundaries

### `npm cache verify`

The vendor command may integrity-check and garbage-collect `_cacache`. DevClean
therefore proves protected-path non-overlap against the exact reviewed
`_cacache` path before invoking `verify`.

The process-idle guard runs before the configuration proof and again immediately
before mutation so the configuration subprocess does not create a stale
concurrency decision.

### `npm cache clean --force`

The current npm implementation recursively removes `flatOptions.cache`, which is
`<base>/_cacache`. DevClean proves protected-path non-overlap against that exact
reviewed content-cache path before invoking the fixed vendor command.

Existing reviewed content keys/size/file-count stability checks remain intact.

### exact `npm cache npx rm <full-key>`

The existing two vendor dry-runs remain mandatory and must resolve to the same
reviewed exact entry path.

After the second dry-run and immediately before the real rm, DevClean now:

1. refreshes the npm/npx process-idle decision;
2. proves protected-path non-overlap against that exact npx entry path;
3. refreshes process-idle state again;
4. invokes the same full-key vendor command.

There is still no abbreviated-key authority and no `--force` whole-npx-cache
operation.

## What this PR deliberately does not change

This audit does not:

- convert npm cache directories into raw TOOL filesystem roots;
- change npm `_logs` from USER_REVIEW;
- change `_cacache`, `_npx`, or `_tuf` rule ownership;
- add any whole-tree application cleanup authority;
- add npm generic-scan pruning;
- surface the dormant maintenance dialog as a second product workflow;
- treat cache occupancy as exact reclaim for partial vendor GC.

The generic-scan performance question is intentionally deferred to a separate
audit/PR so pruning cannot obscure this mutation-safety correction.

## Regression coverage

Focused tests now require:

- normal persistent paths outside `_cacache` to pass the scope proof;
- redirected `prefix`, `userconfig`, or `logs-dir` inside `_cacache` to fail;
- redirected diagnostic state inside an exact npx entry to fail;
- incomplete vendor configuration output to fail closed;
- cache retargeting immediately before mutation to fail;
- `verify` and `clean` to wire the guard to the exact content-cache root;
- exact npx removal to perform the scope/process rechecks after the fresh dry-run
  and immediately before the real rm.

## Merge gate

Merge only from the exact final PR head after the normal DevClean gate is green:
lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows
EXE build/upload, and CodeQL.
