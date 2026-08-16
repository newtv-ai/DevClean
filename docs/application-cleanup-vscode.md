# VS Code cleanup semantics

This document records the storage semantics behind DevClean's VS Code profile.
It is intentionally narrower than a list of folders whose names look disposable.

## Root discovery

VS Code can use several independent storage layouts on Windows:

- Stable user data: `%APPDATA%\Code`
- Insiders user data: `%APPDATA%\Code - Insiders`
- Stable extensions: `%USERPROFILE%\.vscode\extensions`
- Insiders extensions: `%USERPROFILE%\.vscode-insiders\extensions`
- Portable mode: `VSCODE_PORTABLE\user-data`, `VSCODE_PORTABLE\extensions`, and `VSCODE_PORTABLE\tmp`
- Explicit user-data and extension directories supplied by command-line options

DevClean also inspects running `Code.exe` command lines for `--user-data-dir` and
`--extensions-dir`, because custom locations are otherwise not discoverable from
the default profile paths.

Upstream basis: VS Code CLI documentation, Portable Mode documentation, and
`src/bootstrap-node.ts` in the Microsoft VS Code repository.

## TOOL-owned reclaimable data

The following exact subtrees are treated as regenerable application data and may
be offered as whole-tree candidates after the VS Code process is closed:

- `Cache`
- `CachedData`
- `CachedConfigurations`
- `CachedProfilesData`
- `Code Cache`
- `GPUCache`
- `DawnCache`
- `CachedExtensionVSIXs`
- `logs`
- `Crashpad\reports`
- `Crashpad\pending`
- portable `data\tmp`

Age affects whether deletion is worthwhile; it does not make these paths safe or
unsafe. Large cache trees can become worthwhile sooner because DevClean's benefit
model considers reclaim size and rebuild cost.

## USER-owned data

`User\workspaceStorage` is not a cache root. It contains workspace-local state and
can include chat session bodies under `chatSessions`. `User\History` is local file
history. These locations may be large, but generic file deletion and AI-learned
rules do not receive authority over them.

For chat cleanup, DevClean exposes the size and points the user to VS Code's own
chat/session deletion UI. Conversation deletion is treated as a user action, not
cache eviction.

## KEEP state

The following are deliberately protected from generic cleanup:

- `Backups` (unsaved editor / Hot Exit recovery)
- `User` state outside more-specific USER paths, including `globalStorage`
- installed extension roots
- any unclassified data inside a VS Code user-data root

This default-deny boundary prevents a generic suffix such as `.db`, `.json`,
`.log`, or a directory named `cache` inside extension/workspace state from
silently escalating to delete authority.

## Whole-tree rule precedence

An audited application rule may upgrade a legacy `MANUAL_REVIEW` entry for the
same physical cache directory. A report-only or unknown application root never
downgrades an already-audited cleanup root. This keeps source-backed application
semantics authoritative over older name-based heuristics.
