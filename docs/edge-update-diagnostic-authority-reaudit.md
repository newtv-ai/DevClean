# Microsoft Edge Update diagnostic authority re-audit — 2026-08

## Primary source

Microsoft Learn, **Troubleshoot Edge Update, Installation, and Rollback Failures** (current 2026 guidance):

- https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/manageability/update-install-rollback-failures
- https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/development/webview2-unexpected-install-windows-server

Microsoft explicitly instructs customers to collect `MicrosoftEdgeUpdate.log`, its rotated `.bak` when present, and `msedge_installer.log` as diagnostic evidence for update/install/support failures. Current guidance does not establish a seven-day or 256 KiB deletion contract for those files.

## Finding

DevClean previously granted raw TOOL deletion to these exact diagnostic files after a locally invented 7-day threshold. Exact identification proves that the files are Edge diagnostics; it does not prove that old diagnostics are disposable. Recent Microsoft support guidance actively asks users to retain and submit them when troubleshooting.

## Correction

- `MicrosoftEdgeUpdate.log`: KEEP / protected diagnostic evidence.
- `MicrosoftEdgeUpdate.log.bak`: KEEP / protected rotated diagnostic evidence.
- `msedge_installer.log`: KEEP / protected installation diagnostic evidence.
- Edge updater binaries/state remain protected.
- Chromium-derived browser cache semantics remain unchanged.
- Age, size, process-idle state, AI verdicts and user verdicts cannot restore raw deletion authority.

## Revisit

Only expose deletion if Microsoft documents a bounded cleanup operation or explicit expiration/removal lifecycle for one of these exact diagnostics.
