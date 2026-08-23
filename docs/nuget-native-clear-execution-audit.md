# Native NuGet local clear execution audit

Audited: 2026-08-23

## Product conclusion

This re-audit does **not** change NuGet's decision lanes.

- `http-cache`, `temp`, and `plugins-cache` remain `DETERMINISTIC_CANDIDATE` vendor-maintained resources. Their exact selected local-resource roots remain eligible for the quantified safe-cleanup surface.
- `global-packages` remains `USER_REVIEW`: PackageReference projects consume restored dependencies directly from this folder, so clearing it has a user-dependent redownload/restore cost even though NuGet provides a supported clear command.
- No NuGet local resource receives raw filesystem deletion authority from DevClean.

Age, size, and recommendation thresholds remain benefit metadata only. They neither create nor revoke cleanup authority.

## Current NuGet source boundary

The current NuGet.Client source snapshot audited here is `ebc9615813b3d475f848339310f1ced8e9fc1182`.

`LocalsCommandRunner` defines `http-cache`, `global-packages`, `temp`, and `plugins-cache` as separate local resources. An individual `locals <kind> --clear` dispatches only the selected resource and passes its effective path to `ClearCacheDirectory`; DevClean never invokes `all --clear`.

`ClearCacheDirectory` delegates to `LocalResourceUtils.DeleteDirectoryTree`. The current implementation first attempts `Directory.Delete(folderPath, recursive: true)` and its fallback also removes the directory tree. Therefore a successful individual clear targets the whole selected vendor root, including the root directory itself. This is materially different from partial-GC commands such as `uv cache prune`, and it supports treating the selected deterministic NuGet root occupancy as the pre-clean target size.

Current configuration source also confirms the path overrides DevClean uses:

- `NUGET_HTTP_CACHE_PATH` controls the HTTP cache root;
- `NUGET_PLUGINS_CACHE_PATH` controls the plugins cache root;
- `NUGET_PACKAGES` controls global packages;
- `NUGET_SCRATCH` controls the NuGet temporary/scratch root.

When normal live discovery is available, DevClean already asks `dotnet nuget locals all --list --force-english-output` for the effective roots instead of relying only on defaults.

## Execution contract after this re-audit

For any supported NuGet local clear, DevClean now:

1. requires the requested path to equal the audited root for that exact resource kind;
2. refuses if the root is missing;
3. requires the root to be on a local fixed disk with stable filesystem identity and rejects symlink/junction/reparse/cloud-placeholder roots;
4. resolves one exact `dotnet` executable and binds its stable local-fixed filesystem identity;
5. pins the documented environment override for the selected resource to the reviewed exact root;
6. asks that exact executable to run `dotnet nuget locals <kind> --list --force-english-output` and requires exactly the same absolute root;
7. rechecks NuGet/.NET restore/build activity immediately before mutation;
8. rechecks the exact `dotnet` identity and selected-root identity immediately before mutation;
9. repeats the vendor `--list` root confirmation immediately before mutation;
10. runs one fixed command: `dotnet nuget locals <kind> --clear --force-english-output` through the already-bound absolute executable;
11. propagates vendor failure and never falls back to direct deletion;
12. revalidates the `dotnet` executable after the command;
13. requires the selected root to be absent after a successful vendor exit, matching current NuGet `DeleteDirectoryTree` semantics;
14. reports the actual absolute command and measured logical before/after result.

If NuGet returns success but leaves or recreates the reviewed root, DevClean treats the postcondition as unproven and reports failure. It does not finish the clear with raw filesystem deletion.

## Why deterministic quantification remains valid

This audit specifically checked whether NuGet had the same quantification problem found in the pip re-audit. It does not.

Current pip purge operates on pip-recognized cache items and does not promise recursive deletion of every arbitrary byte under a custom cache root. Current NuGet individual local clear, by contrast, passes the selected resource root to a recursive directory-tree deletion operation. For `http-cache`, `temp`, and `plugins-cache`, the exact reviewed root is therefore the vendor mutation target itself.

The distinction is source-based, not name-based: DevClean does not generalize this authority to `global-packages`, unknown NuGet folders, configuration files, project metadata, or arbitrary directories named `cache`.

## Primary sources

- NuGet.Client `LocalsCommandRunner.cs` at `ebc9615813b3d475f848339310f1ced8e9fc1182`: https://github.com/NuGet/NuGet.Client/blob/ebc9615813b3d475f848339310f1ced8e9fc1182/src/NuGet.Core/NuGet.Commands/CommandRunners/LocalsCommandRunner.cs
- NuGet.Client `LocalResourceUtils.cs` at the same snapshot: https://github.com/NuGet/NuGet.Client/blob/ebc9615813b3d475f848339310f1ced8e9fc1182/src/NuGet.Core/NuGet.Common/LocalResourceUtils.cs
- NuGet.Client `SettingsUtility.cs` at the same snapshot: https://github.com/NuGet/NuGet.Client/blob/ebc9615813b3d475f848339310f1ced8e9fc1182/src/NuGet.Core/NuGet.Configuration/Utility/SettingsUtility.cs
- NuGet.Client `NuGetEnvironment.cs` at the same snapshot: https://github.com/NuGet/NuGet.Client/blob/ebc9615813b3d475f848339310f1ced8e9fc1182/src/NuGet.Core/NuGet.Common/PathUtil/NuGetEnvironment.cs
