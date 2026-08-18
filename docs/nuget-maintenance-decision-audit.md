# NuGet maintenance decision audit

Audited: 2026-08-18

## Product conclusion

NuGet local storage is not one undifferentiated cache. DevClean therefore makes the cheapest stable decision it can and does not send these known resources to AI.

| Resource | DevClean lane | Default selection | Execution |
| --- | --- | --- | --- |
| `http-cache` | deterministic candidate | when at least 64 MiB | `dotnet nuget locals http-cache --clear` |
| `temp` | deterministic candidate | when at least 16 MiB | `dotnet nuget locals temp --clear` |
| `plugins-cache` | deterministic candidate | when at least 16 MiB | `dotnet nuget locals plugins-cache --clear` |
| `global-packages` | user review | never | `dotnet nuget locals global-packages --clear` only after explicit choice |

The size thresholds are benefit thresholds, not safety claims. A smaller documented cache is still locally understood and may be selected by the user, but DevClean does not preselect low-value churn.

## Why three resources are deterministic

Microsoft documents `http-cache` as cached NuGet feed communication, `temp` as temporary NuGet operation storage, and `plugins-cache` as cached plugin operation-claim results. The .NET CLI exposes each as a named `dotnet nuget locals` cache location and documents `--clear` as the operation that recursively clears the selected cache contents.

DevClean delegates to that vendor command rather than deleting arbitrary files by path. Before running it, the implementation re-resolves the audited root, requires an exact root match, and refuses while NuGet/.NET restore, build, MSBuild, or Visual Studio activity is detected. Vendor failure is surfaced with no raw-delete fallback.

## Why global-packages stays with the user

Microsoft documents that PackageReference projects consume packages directly from `global-packages`. Clearing the folder is supported, but projects must restore again to re-download required packages; Visual Studio may also need the solution reloaded or a command-line restore.

That makes the operation technically supported but not universally beneficial. A developer who needs offline or immediately available dependencies may prefer the disk usage. DevClean therefore inventories the bytes and explains the tradeoff locally, but never preselects this resource and never asks AI to guess the user's intent.

## Custom locations

NuGet supports environment overrides for all four resources. The maintenance command receives the exact resolved path through the corresponding NuGet environment variable and validates that the selected path is still the effective audited root before execution. This avoids assuming the documented Windows defaults when a machine is configured differently.

## Sources

- Microsoft Learn, **How to manage the global packages, HTTP cache, temp folders in NuGet**: documents Windows locations, overrides, resource semantics, direct PackageReference use of `global-packages`, restore requirements after clearing it, and supported clear commands.
- Microsoft Learn, **dotnet nuget locals command**: documents `http-cache`, `global-packages`, `temp`, `plugins-cache`, `--list`, `--clear`, and recursive cache-content clearing.
