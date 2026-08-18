# Visual Studio Installer package-cache audit

DevClean treats the Visual Studio Installer package cache as servicing state with optional retained payloads, not as a generic download folder that can be recursively deleted by filename or age.

Source-audited boundaries:

- Microsoft documents `%ProgramData%\Microsoft\VisualStudio\Packages` as the default `CachePath` for package manifests and payloads.
- Microsoft documents exact machine-wide policy precedence for Visual Studio setup values: `HKLM\SOFTWARE\Policies\Microsoft\VisualStudio\Setup`, then `HKLM\SOFTWARE\Microsoft\VisualStudio\Setup`, then `HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\Setup`. Visual Studio stops at the first discovered policy value. DevClean now reads `CachePath` in that same order instead of guessing redirected locations from drive names or nearby folders.
- `CachePath` is documented as `REG_SZ` or `REG_EXPAND_SZ`. DevClean accepts an absolute `REG_SZ`, expands a `REG_EXPAND_SZ` only from known environment variables, and fails closed on relative paths, unresolved variables, unsupported registry types, or inconclusive registry access. A registry read failure never silently falls back to the default cache.
- If no `CachePath` value exists in any documented policy location, DevClean uses the documented `%ProgramData%\Microsoft\VisualStudio\Packages` default. Changing `CachePath` after installation requires moving and securing the existing cache correctly; otherwise future setup operations can fail.
- `KeepDownloadedPayloads` defaults to enabled. Microsoft documents `--nocache` as an installer operation mode: an install/modify/repair operation removes existing packages for that product and avoids retaining subsequent payloads. This is not a source-audited standalone garbage-collection command for arbitrary files inside the cache.
- Cached packages provide a source for repair and related servicing, especially when offline. When payload retention is disabled, the download-cache location can still retain package metadata after installation, so an apparently small cache is not evidence that the root itself is disposable.
- `%ProgramData%\Microsoft\VisualStudio\Packages\_Instances\<InstanceID>\state.json` records installation source state for local-layout installs. Microsoft warns that future updates or component additions can depend on the recorded layout path. DevClean therefore gives `_Instances` a more-specific protected rule instead of treating it as download residue. When `CachePath` is redirected, the same protected `_Instances` boundary follows the effective cache root.
- The Visual Studio Installer itself lives under `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer`, while Visual Studio instances, shared components, SDKs, and toolchains are installed under separately configured installation roots. Those are installed product state and are deliberately outside this cache rule.
- NuGet local storage is already covered by DevClean's separate NuGet audit and is not duplicated here.
- MSBuild output is project-defined. Microsoft documents configurable `OutputPath`/`OutDir` and `IntermediateOutputPath`/`IntDir`, and the supported cleanup boundary is a project-aware `Clean` target. DevClean therefore does not globally infer that arbitrary `bin`, `obj`, or similarly named directories are safe to delete.

Conclusion: the effective Visual Studio Installer package cache is `INSTALLERS_DOWNLOADS` / REPORT_ONLY / KEEP, with `_Instances` explicitly protected as servicing metadata. DevClean grants zero raw file or whole-tree deletion authority and does not automate `--nocache`, because that switch belongs to a concrete installer operation rather than a standalone cache-prune contract.

Official references audited on 2026-08-18:

- https://learn.microsoft.com/en-us/visualstudio/install/disable-or-move-the-package-cache?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/configure-policies-for-enterprise-deployments?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/change-installation-locations?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/create-an-offline-installation-of-visual-studio?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/msbuild/how-to-clean-a-build?view=vs-2022
- https://learn.microsoft.com/en-us/visualstudio/ide/how-to-change-the-build-output-directory?view=visualstudio
