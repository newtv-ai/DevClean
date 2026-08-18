# .NET SDK workload cleanup audit

DevClean delegates workload garbage collection to the installed .NET SDK instead of deleting workload pack directories by path.

Source-audited boundaries:

- Microsoft documents `dotnet workload clean` as the command for removing workload components left behind by previous SDK workload updates and uninstallations.
- Microsoft's .NET 8 SDK notes describe the plain command as workload garbage collection for orphaned packs. Orphaned packs include packs from uninstalled SDK versions or packs whose installation records no longer exist.
- The same source distinguishes `dotnet workload clean --all` as a more aggressive mode that removes every pack of the current SDK workload installation type and removes workload installation records for the running SDK feature band and below.
- Microsoft also documents that when Visual Studio owns workload components, the command reports workloads that should instead be cleaned through Visual Studio rather than silently treating Visual Studio-managed content as ordinary file-system garbage.
- DevClean therefore exposes only plain `dotnet workload clean`. It intentionally does **not** expose `--all` in this maintenance action.
- The action delegates all pack-selection logic to the vendor CLI. DevClean does not infer SDK pack directories, installation-record locations, MSI ownership, or Visual Studio workload ownership from names or age.
- Before invoking the command, DevClean conservatively refuses to proceed while `dotnet`, `msbuild`, or `devenv` is running. Process-query failure is treated as in-use.
- Vendor command failures are surfaced to the user and do not trigger a raw-file fallback.

Conclusion: .NET workload cleanup is a vendor-managed maintenance operation, not a generic recursive-delete rule. The safe default is `dotnet workload clean`; the aggressive `--all` mode remains outside DevClean's public maintenance surface.

Official Microsoft references audited on 2026-08-18:

- https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-workload-clean
- https://learn.microsoft.com/en-us/dotnet/core/whats-new/dotnet-8/sdk#dotnet-workload-clean-command
