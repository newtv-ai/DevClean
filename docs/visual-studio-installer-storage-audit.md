# Visual Studio Installer package-cache audit

DevClean treats the Visual Studio Installer package cache as servicing state with optional retained payloads, not as a generic download folder that can be recursively deleted by filename or age.

Source-audited boundaries:

- Microsoft documents `%ProgramData%\Microsoft\VisualStudio\Packages` as the default `CachePath` for package manifests and payloads.
- The cache can be moved by the machine-wide `CachePath` registry policy. Changing that policy after installation requires moving the existing cache correctly; otherwise future setup operations can fail. DevClean therefore does not guess redirected cache locations from drive names or nearby folders.
- `KeepDownloadedPayloads` defaults to enabled. Microsoft documents `--nocache` as an installer operation mode: an install/modify/repair operation removes existing packages for that product and avoids retaining subsequent payloads. This is not a source-audited standalone garbage-collection command for arbitrary files inside the cache.
- Cached packages provide a source for repair and related servicing, especially when offline. When payload retention is disabled, the download-cache location can still retain package metadata after installation, so an apparently small cache is not evidence that the root itself is disposable.
- `%ProgramData%\Microsoft\VisualStudio\Packages\_Instances\<InstanceID>\state.json` records installation source state for local-layout installs. Microsoft warns that future updates or component additions can depend on the recorded layout path. DevClean therefore gives `_Instances` a more-specific protected rule instead of treating it as download residue.
- The Visual Studio Installer itself lives under `%ProgramFiles(x86)%\Microsoft Visual Studio\Installer`, while Visual Studio instances, shared components, SDKs, and toolchains are installed under separately configured installation roots. Those are installed product state and are deliberately outside this cache rule.
- NuGet local storage is already covered by DevClean's separate NuGet audit and is not duplicated here.
- MSBuild output is project-defined. Microsoft documents configurable `OutputPath`/`OutDir` and `IntermediateOutputPath`/`IntDir`, and the supported cleanup boundary is a project-aware `Clean` target. DevClean therefore does not globally infer that arbitrary `bin`, `obj`, or similarly named directories are safe to delete.
- The current application facade has no registry-policy discovery adapter. This audit inventories the documented default package cache only when `PROGRAMDATA` is available. A redirected `CachePath` stays fail-closed until its effective registry value can be read authoritatively.

Conclusion: the documented default Visual Studio Installer package cache is `INSTALLERS_DOWNLOADS` / REPORT_ONLY / KEEP, with `_Instances` explicitly protected as servicing metadata. DevClean grants zero raw file or whole-tree deletion authority and does not automate `--nocache`, because that switch belongs to a concrete installer operation rather than a standalone cache-prune contract.

Official references audited on 2026-08-18:

- https://learn.microsoft.com/en-us/visualstudio/install/disable-or-move-the-package-cache?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/configure-policies-for-enterprise-deployments?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/change-installation-locations?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/install/create-an-offline-installation-of-visual-studio?view=visualstudio
- https://learn.microsoft.com/en-us/visualstudio/msbuild/how-to-clean-a-build?view=vs-2022
- https://learn.microsoft.com/en-us/visualstudio/ide/how-to-change-the-build-output-directory?view=visualstudio
