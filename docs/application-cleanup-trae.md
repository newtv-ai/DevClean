# Trae cleanup semantics

Trae is VS Code/Electron-derived, but its proprietary AI/session/index storage is
not documented with the same precision as VS Code or Cursor. DevClean therefore
uses a default-deny application profile: only well-understood runtime caches,
logs, and crash reports are delegated to generic cleanup. Everything else is
protected until a stronger vendor/source signal exists.

## Candidate roots

DevClean probes roots only when they exist on the machine. The current Windows
profile considers the common Trae/Trae CN application-data names under roaming
and local AppData, `%USERPROFILE%\.trae`, explicit DevClean discovery overrides,
and `--user-data-dir` / `--extensions-dir` values observed on running Trae
processes. A candidate root being discovered never means its whole tree is
deletable.

## TOOL-owned data

Inside an identified Trae user-data root, the following exact Electron runtime
subtrees are treated as regenerable and may become whole-tree cleanup candidates
once Trae is closed:

- `Cache`
- `CachedData`
- `Code Cache`
- `GPUCache`
- `DawnCache`
- `CachedExtensions`
- `CachedExtensionVSIXs`
- `logs`
- `Crashpad\reports`
- `Crashpad\pending`

Age is a utility/reuse signal, not a safety boundary. Reclaim size and rebuild
cost are included in the same benefit model used by Codex, Claude Code, Cursor,
and VS Code.

## USER-owned data

`User\workspaceStorage` and `User\History` are protected as user data. Trae may
layer proprietary AI/session state onto workspace storage, so DevClean does not
assume that a database, JSONL file, `cache`-named child, or old timestamp makes
that state regenerable.

## KEEP state

The following remain outside generic delete authority:

- `User` state outside the more-specific USER paths, including `globalStorage`
- `Backups` / editor recovery data
- installed extensions
- `%USERPROFILE%\.trae` persistent data
- every unclassified file or directory inside a Trae application-data root

This deliberately prevents generic `.db`, `.sqlite`, `.json`, `.log`, `.tmp`, or
`cache` heuristics from overriding application semantics.

## Upgrade rule

If future Trae documentation or a stable vendor command proves that a specific AI
index, chat archive, plugin cache, or other subtree is completely regenerable,
that exact subtree can be promoted from KEEP/USER to TOOL. DevClean should not
promote a whole parent root merely because one child becomes reclaimable.
