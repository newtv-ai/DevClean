# npm persistent-path overlap follow-up audit — 2026-08

## Finding

The mutation-scope hardening merged in #197 remains valid: before invoking npm's
recursive cache operations, DevClean re-confirms the reviewed cache and refuses
execution when the npm global prefix, user config, diagnostic logs, or default
`_logs` area falls inside the mutation range.

While preparing a later generic-scan performance audit, a broader review of npm's
current configuration schema found additional long-lived path-valued inputs that
must receive the same protection. They do not change npm cache ownership, but
placing one of them inside `_cacache` or an exact npx entry would make the vendor
recursive operation broader than the authority DevClean presents.

The audited current npm source is npm 12.0.2 at commit
`dc43591e6e08e9857c787116b1ed12f074e68c3c`.

Relevant current source semantics include:

- `globalconfig` is a path to the global npm configuration file;
- `global-ignore-file` is a path to user-owned global pack/publish ignore rules;
- `cafile` is a path to a CA trust file;
- `init-module` and its historical `init.module` alias are paths to the user's
  npm-init template module;
- `node-gyp` is a path to the node-gyp executable npm may invoke;
- `prefix`, `userconfig`, and `logs-dir` retain the protection added in #197.

npm's config loader computes the normal `globalconfig` default from the current
prefix. npm 12 likewise computes the normal `global-ignore-file` default from the
current prefix, so these are real persistent paths rather than names inferred by
DevClean.

## Compatibility audit

The follow-up must not make an older, otherwise supported npm fail merely because
a newer npm introduced a configuration key.

npm 11.6.2 source was therefore checked separately. The stable protected inputs
used here (`globalconfig`, `cafile`, `init-module`/`init.module`, `node-gyp`, plus
the #197 keys) are present there. `global-ignore-file` is not part of that audited
v11 schema but is present in npm 12.

DevClean now asks the already-bound npm executable for its major version before
the boundary query:

- npm major 11 and earlier use only the stable audited boundary-key set;
- npm major 12 and later additionally query `global-ignore-file`.

If the version cannot be proven, or the requested boundary keys are not returned,
DevClean fails closed rather than guessing.

## Correction

The existing `_require_no_protected_overlap()` execution guard is extended; no
new mutation path or cleanup rule is added.

For every guarded npm vendor mutation, DevClean uses the same reviewed npm CLI and
the same pinned cache environment to:

1. obtain and parse the npm major version;
2. query the version-appropriate boundary keys;
3. require the reviewed base cache to be confirmed again;
4. add the following source-backed persistent/security paths to the overlap
   protection set when configured:
   - global prefix;
   - user config;
   - global config;
   - CA file;
   - npm-init module and its historical alias;
   - node-gyp executable;
   - configured logs-dir;
   - normal `<cache>/_logs` diagnostics;
   - npm 12+ global ignore file;
5. refuse the vendor operation if any protected path is equal to or inside the
   exact mutation root.

The existing process rechecks, CLI/cache identity binding, exact npx dry-run path
proof, fixed vendor commands, postconditions, and no-filesystem-fallback behavior
are unchanged.

## Scope boundary

This follow-up intentionally protects the audited globally effective top-level
persistent/security path inputs above. It does **not** claim to enumerate every
possible path string npm can encounter in every project, command, package spec,
or registry-scoped configuration key.

Examples such as one-off project-local command inputs, package file specs, or
registry-scoped certificate/key settings have different scopes and cannot be
safely turned into global cleanup ownership merely by discovering a path value.
This PR neither grants nor removes authority for them.

## What this PR does not change

This follow-up does not:

- change `_cacache`, `_npx`, `_tuf`, or `_logs` rule ownership;
- create a raw filesystem TOOL root;
- change USER/KEEP/TOOL/AI lanes;
- add any whole-tree generic deletion authority;
- change the npm maintenance UI or create a second product workflow;
- add generic-scan pruning;
- treat provider-root occupancy as exact reclaim for partial vendor GC.

The npm generic-scan performance audit remains a separate follow-up so traversal
optimization cannot obscure mutation-safety review.

## Regression coverage

Focused Windows tests require:

- npm 11 to query the stable boundary set without requesting the npm-12-only
  `global-ignore-file` key;
- npm 12 to query and protect `global-ignore-file`;
- each stable persistent/security path to block a mutation when redirected inside
  `_cacache`;
- the same protection to apply to an exact npx entry range;
- nullable optional paths to remain valid when npm reports them as null;
- incomplete boundary output or an unprovable npm major to fail closed;
- cache retargeting immediately before mutation to remain rejected.

## Merge gate

Merge only from the exact final PR head after the normal DevClean gate is green:
lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows
EXE build/upload, and CodeQL.
