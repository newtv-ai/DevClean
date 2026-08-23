# Visual Studio IDE storage re-audit

Audited again: 2026-08-23

DevClean treats Visual Studio's user-data tree as mixed IDE state. Only exact
subtrees with source-backed regeneration semantics receive deterministic
deletion authority.

## Exact TOOL cache boundaries

### ComponentModelCache

Current Visual Studio documentation places MEF composition state and the
composition error log inside
`%LOCALAPPDATA%\Microsoft\VisualStudio\<instance>\ComponentModelCache`.
Microsoft troubleshooting guidance explicitly instructs users to close Visual
Studio, delete or rename the entire `ComponentModelCache`, and restart Visual
Studio so the cache is rebuilt.

DevClean therefore recognizes only existing Visual Studio 2019/2022/2026
instance selectors (`16.0`, `17.0`, `18.0`, with optional hexadecimal instance
suffixes) under `%LOCALAPPDATA%\Microsoft\VisualStudio` and grants TOOL authority
only to the exact `ComponentModelCache` subtree.

The diagnostic `.err` file inside this cache does not widen or revoke that
boundary. It is derived MEF composition evidence inside a vendor-supported
whole-cache reset operation, not an independently retained user-history root.

### Roslyn analyzer cache

Microsoft staff support guidance identifies
`%LOCALAPPDATA%\Microsoft\VisualStudio\Roslyn\Cache` as Roslyn analyzer cache and
states that deleting it does not affect Visual Studio functionality, although
analyzer loading can be slower while the cache is rebuilt.

DevClean deliberately narrows that statement to the exact shared
`Roslyn\Cache` subtree. The parent `Roslyn` directory and unknown siblings are
not claimed.

## Second-pass safety/benefit correction

The original cache rules attached conservative age and minimum-reclaim values
to these exact TOOL roots. That was useful for ranking, but the per-path
evaluator later used those values to revoke the already-proven safety decision.
A fresh/tiny cache, or one whose last-use timestamp is unavailable, could be
returned as `TOOL_KEEP_RECENT`, `TOOL_KEEP_LOW_BENEFIT`, or
`TOOL_KEEP_UNKNOWN_USAGE`.

That behavior is corrected in this re-audit.

For exact `ComponentModelCache` and `Roslyn\Cache` objects:

- Visual Studio/ServiceHub active -> `TOOL_KEEP_IN_USE` remains a hard live
  execution blocker;
- owning processes closed -> `TOOL_DELETE` regardless of age, size, or missing
  last-use metadata;
- the existing idle/min-reclaim/rebuild-cost fields remain available for UI
  benefit scoring and ranking only.

This does not convert a generic directory named `Cache` into TOOL data. Exact
Visual Studio root identity still establishes authority first.

## ServiceHub diagnostic logs are USER, not cache

`%TEMP%\servicehub\logs` is a separate diagnostic lane.

Microsoft's performance-troubleshooting instructions tell users to delete this
folder before reproducing a directly reproducible out-of-process issue, but the
same guidance says to keep the folder intact when the issue cannot be
reproduced. The directory is therefore technically understood and manually
removable, while its retention value is user-dependent.

Current policy:

- semantic owner: `USER`;
- review action: `USER_DECISION`;
- catalog: `SYSTEM_LOGS` / `REPORT_ONLY`;
- no deterministic whole-tree deletion authority;
- a user-approved mutation still requires Visual Studio and ServiceHub
  processes to be closed.

Age and size do not manufacture TOOL authority for diagnostic evidence. See
`docs/visual-studio-servicehub-logs-audit.md` for the focused evidence review.

## Protected Visual Studio state

Visual Studio SDK documentation describes its local-settings application-data
area as user-specific local files and distinguishes it from roaming settings,
documents, and extension roots. No authoritative Microsoft product contract was
found that narrows per-instance
`%LOCALAPPDATA%\Microsoft\VisualStudio\<instance>\WebTools` to disposable
cache-only data or provides an exact supported rebuild operation for that tree.

Therefore:

- `WebTools` remains `IDE_CACHE` / `REPORT_ONLY` / KEEP;
- `%LOCALAPPDATA%\Microsoft\VisualStudio\Packages` remains installer servicing
  state / KEEP;
- Visual Studio Installer package cache and `_Instances` metadata remain KEEP;
- the surrounding per-instance Visual Studio directory receives no recursive
  deletion authority;
- files such as `privateregistry.bin`, settings, licensing state, extensions,
  and unknown siblings are not claimed;
- `%APPDATA%\Microsoft\VisualStudio\<instance>\ActivityLog.xml` remains
  troubleshooting evidence outside the cache rules;
- project-local `.vs`, `bin`, and `obj` directories remain project-owned and are
  not inferred globally from their names.

Process-query failures fail closed.

## Conclusion

Exact per-instance `ComponentModelCache` roots and the exact shared
`Roslyn\Cache` root remain `IDE_CACHE` / VENDOR_MANAGED / TOOL. Their safety is
source-backed and no longer revoked by age/size/last-use heuristics.

ServiceHub logs move to USER_REVIEW. WebTools, per-user Packages, installer
cache/metadata, project outputs, and surrounding Visual Studio state remain
protected or separately managed.

Official Microsoft references audited/rechecked through 2026-08-23:

- https://learn.microsoft.com/en-us/visualstudio/extensibility/managed-extensibility-framework-in-the-editor?view=visualstudio
- https://learn.microsoft.com/en-us/answers/questions/2115313/im-running-windows-11-24h2-and-using-visual-studio
- https://learn.microsoft.com/en-us/answers/questions/483037/can-i-safely-delete-appdatalocalmicrosoftvisualstu
- https://learn.microsoft.com/en-us/answers/questions/1221136/visual-studio-2022-clear-local-caches
- https://learn.microsoft.com/en-us/dotnet/api/microsoft.visualstudio.settings.applicationdatafolder?view=visualstudiosdk-2022
- https://learn.microsoft.com/en-us/visualstudio/ide/reference/log-devenv-exe?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/ide/how-to-increase-chances-of-performance-issue-being-fixed?view=visualstudio
