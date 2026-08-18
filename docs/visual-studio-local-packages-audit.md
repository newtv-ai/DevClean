# Visual Studio per-user Packages audit

DevClean treats `%LOCALAPPDATA%\Microsoft\VisualStudio\Packages` as setup/update servicing state, not as a generic download cache.

Source-audited boundaries:

- Microsoft Visual Studio installation troubleshooting documentation states that Visual Studio creates `_channels` during initial installation under both `C:\ProgramData\Microsoft\VisualStudio\Packages` and `%LOCALAPPDATA%\Microsoft\VisualStudio\Packages`.
- The same documentation states that the local Visual Studio folder hosts channel manifest data containing product and upgrade details, and that Visual Studio compares catalog and channel manifest files from the ProgramData and per-user locations during update.
- Microsoft documents that missing or corrupt per-user Visual Studio content can prevent channel initialization during an upgrade, which makes this state operationally relevant rather than disposable by default.
- Microsoft documents a concrete repair path when that per-user state is missing: copy `_channels` from the ProgramData package location back into `%LOCALAPPDATA%\Microsoft\VisualStudio\Packages` before retrying the update.
- Those semantics are evidence that at least part of the per-user `Packages` tree participates directly in installer servicing. They are not an authoritative contract that unknown siblings or old files inside that root can be recursively deleted.
- DevClean therefore inventories the exact per-user `Packages` root as `INSTALLERS_DOWNLOADS` / REPORT_ONLY / KEEP. Age and size never convert the root to TOOL ownership, and no raw-file or whole-tree deletion authority is exposed.
- This rule is separate from the machine-wide Visual Studio Installer package-cache audit under `%ProgramData%\Microsoft\VisualStudio\Packages`. The two locations have different roles and both remain fail-closed unless a narrower vendor-supported disposable boundary is established.

Conclusion: `%LOCALAPPDATA%\Microsoft\VisualStudio\Packages` is visible for disk accounting but protected as installer servicing state. In particular, `_channels` must not be inferred to be junk merely because it contains downloaded manifests.

Official Microsoft reference audited on 2026-08-18:

- https://learn.microsoft.com/en-us/troubleshoot/developer/visualstudio/installation/error-test-deploy-minimal-layout
