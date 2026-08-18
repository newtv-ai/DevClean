# .NET global tools storage audit

Audited: 2026-08-18

## Product decision

.NET global tools are **USER_REVIEW**, not automatic cache cleanup and not default AI review.

DevClean can identify them precisely, explain what they are, show the total storage used by the documented global-tool root, and let the user remove an individual tool through the .NET CLI. Whether a working installed tool is still useful is personal intent, so DevClean must not make that decision for every user.

## Authoritative Microsoft boundary

Microsoft documents the default Windows global-tool location as:

`%USERPROFILE%\.dotnet\tools`

The command shims live in that directory and the actual tool binaries are nested under the sibling `.store` directory. Microsoft also documents `dotnet tool list --global` as the supported way to enumerate the current user's global tools, including package ID, installed version, and commands.

Microsoft's supported removal operation is:

`dotnet tool uninstall --global <PACKAGE_ID>`

The package ID can be obtained from `dotnet tool list`. DevClean therefore treats the installed package ID returned by the vendor command as the authority boundary and does not infer packages by walking `.store`.

Sources:

- https://learn.microsoft.com/en-us/dotnet/core/tools/global-tools
- https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-tool-list
- https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-tool-uninstall
- https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-tool-install

## Safety rules

1. Listing is read-only and may run without deleting anything.
2. DevClean never recursively deletes `%USERPROFILE%\.dotnet\tools` or `.store`.
3. An uninstall request must match exactly one package currently returned by `dotnet tool list --global`.
4. Removal is delegated to `dotnet tool uninstall --global <PACKAGE_ID>`.
5. If `dotnet`, `msbuild`, or Visual Studio is running, removal fails closed.
6. Vendor-command failure is surfaced to the user; there is no raw-file fallback.
7. Unknown files or unexplained residue inside `.dotnet` do not inherit permission from this audit.

## Why this is not deterministic cleanup

A global tool can be fully functional and actively needed. Its storage is reclaimable only if the user no longer wants the tool. That is a simple user-intent question, not a technical ambiguity that justifies paid AI review.

This follows DevClean's review-lane policy:

- universal disposable/rebuildable storage -> DevClean determines it locally;
- known installed software whose usefulness depends on the user -> user decides;
- genuinely unexplained residual files -> AI can be used when local evidence is insufficient;
- protected state -> report only.

## Deliberate non-scope

This audit does not automatically manage `--tool-path` installations because Microsoft allows those tools to live at arbitrary user-selected paths. It also does not remove local tools from project manifests. Those need project- or path-specific context and should be audited separately.
