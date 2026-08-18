# Visual Studio ServiceHub log audit

DevClean treats `%TEMP%\servicehub\logs` as a source-backed Visual Studio diagnostic-log cleanup boundary.

Source-audited boundaries:

- Microsoft Visual Studio performance-troubleshooting guidance describes ServiceHub and other satellite processes as out-of-process components that provide features alongside the main Visual Studio process.
- For a directly reproducible out-of-process issue, Microsoft explicitly instructs starting by deleting the `%temp%\servicehub\logs` folder before enabling full ServiceHub tracing and reproducing the issue.
- That instruction is an exact whole-folder regeneration contract for the `logs` subtree. DevClean does not generalize it to the parent `%TEMP%\servicehub` directory or similarly named siblings.
- Recent logs can still be diagnostically valuable. DevClean therefore uses a conservative 14-day idle threshold and a 16 MiB minimum reclaim threshold instead of deleting every discovered log tree immediately.
- Cleanup requires Visual Studio and ServiceHub satellite processes to be closed. Process-query failures fail closed.
- This rule does not affect per-instance Visual Studio state, WebTools, Roslyn siblings, project `.vs` content, or the Visual Studio Installer package caches.

Conclusion: exact `%TEMP%\servicehub\logs` roots are `SYSTEM_LOGS` / VENDOR_MANAGED / TOOL. The containing ServiceHub temp tree receives no recursive deletion authority.

Official Microsoft reference audited on 2026-08-18:

- https://learn.microsoft.com/en-us/visualstudio/ide/how-to-increase-chances-of-performance-issue-being-fixed?view=visualstudio
