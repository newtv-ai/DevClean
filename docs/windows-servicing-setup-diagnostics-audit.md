# Windows servicing and setup diagnostics audit

Audited: 2026-08-20

## Product conclusion

The remaining broad Windows servicing/setup diagnostic trees do **not** earn generic cleanup authority.

Current Microsoft documentation identifies these files as troubleshooting evidence for Windows Setup, Windows Update, Component Based Servicing (CBS), DISM, and upgrade failures. It does not expose a stable per-file expiration/deletion contract comparable to Delivery Optimization's `ExpireOn`, nor a bounded exact vendor delete API comparable to the already-implemented crash-dump and Recycle Bin lanes.

DevClean therefore classifies the following as **REPORT_ONLY / protected diagnostic state** for generic cleanup:

- `%WINDIR%\Panther` Windows Setup logs;
- `%SystemDrive%\$Windows.~BT\Sources\Panther` and `Rollback` setup logs when that upgrade tree still exists;
- `%WINDIR%\Logs\MoSetup` setup/Windows Update communication logs such as `BlueBox.log`;
- `%WINDIR%\Logs\CBS\CBS.log` and archived `CbsPersist_*.log` / `CbsPersist_*.cab`;
- `%WINDIR%\Logs\DISM\dism.log`;
- `%WINDIR%\Logs\SetupDiag\SetupDiagResults.xml` as the automatically generated SetupDiag diagnosis.

This is a deliberate negative audit, not missing coverage. File age, size, `.log`/`.cab` suffixes, or a directory being named `Logs`, `Panther`, `CBS`, `DISM`, or `SetupDiag` do not create deletion authority.

## Primary Microsoft contracts

Current Microsoft documentation establishes:

- Windows Setup writes phase-specific logs to `%WINDIR%\Panther`, `$Windows.~BT\Sources\Panther`, and `$Windows.~BT\Sources\Rollback`, and recommends these logs for diagnosing installation/upgrade failures;
- `setupact.log` is the primary starting point for upgrade failure investigation, with `setuperr.log`, migration logs, setup API logs, event logs, and `BlueBox.log` providing additional diagnostic evidence;
- CBS troubleshooting uses `%WINDIR%\Logs\CBS\CBS.log` and archived `CbsPersist_*.log` / `CbsPersist_*.cab` files;
- DISM troubleshooting uses `%WINDIR%\Logs\DISM\dism.log`;
- SetupDiag is included in supported Windows Setup, automatically analyzes upgrade failures, and writes `%WINDIR%\Logs\SetupDiag\SetupDiagResults.xml` plus registry results.

Primary sources:

- https://learn.microsoft.com/windows/deployment/upgrade/log-files
- https://learn.microsoft.com/troubleshoot/windows-client/setup-upgrade-and-drivers/windows-setup-log-file-locations
- https://learn.microsoft.com/windows-hardware/manufacture/desktop/windows-setup-log-files-and-event-logs?view=windows-11
- https://learn.microsoft.com/windows/deployment/upgrade/setupdiag
- https://learn.microsoft.com/troubleshoot/azure/virtual-machines/windows/troubleshoot-cbs-component-store-corruption-azure-vm
- https://learn.microsoft.com/troubleshoot/windows-server/installing-updates-features-roles/troubleshoot-windows-update-error-0x80070490

## Why archived CBS files are not automatic cleanup

Microsoft explicitly calls `CbsPersist_*.log` and `CbsPersist_*.cab` **archived logs** and continues to recommend collecting them when servicing/update problems cannot be resolved. That proves diagnostic identity, but not a user-independent expiration boundary.

DevClean therefore does not infer:

- `CbsPersist_*.cab` = disposable cache;
- older than N days = safe;
- compressed archive = no longer useful;
- not currently open = vendor-authorized deletion.

A historical or support-oriented log may remain valuable long after the servicing operation that produced it.

## Why Panther / Rollback / MoSetup logs remain protected

Windows Setup documentation assigns different logs to different phases and failure codes. The same setup tree can contain migration information, device-install evidence, event logs, and rollback evidence.

A broad `Panther` or `$Windows.~BT\Sources\Rollback` cleanup rule would therefore remove troubleshooting evidence whose value is user- and incident-specific. DevClean already has a separate, source-backed `Windows.old` lifecycle lane; that does **not** grant raw authority over setup diagnostic subtrees outside the exact vendor operation used there.

## SetupDiag nuance

`%WINDIR%\Logs\SetupDiag\SetupDiagResults.xml` is a particularly well-identified single diagnostic artifact, but it is normally small and exists specifically to explain an upgrade failure. Microsoft documents its generation and diagnostic role, not a retention/deletion lifecycle.

DevClean does not add a dedicated destructive UI for such a low-benefit diagnostic file merely because exact identity is known. This is consistent with the product rule that technical identifiability alone is insufficient when expected disk benefit is negligible and diagnostic value is plausible.

## Explicit exclusions

This audit grants no authority to:

- recursively delete `%WINDIR%\Panther`, `%WINDIR%\Logs`, `%WINDIR%\Logs\CBS`, `%WINDIR%\Logs\DISM`, or `%WINDIR%\Logs\MoSetup`;
- delete `$Windows.~BT` or any of its setup/rollback trees by path convention;
- stop TrustedInstaller, Windows Update, DISM, Setup, or logging services merely to make files deletable;
- take ownership, rewrite ACLs, disable logging, or change CBS/Setup/SetupDiag registry policy;
- delete `CBS.log`, `dism.log`, setup logs, or archived diagnostics based only on age or size;
- use Microsoft Q&A/manual workarounds as generic product authority when current primary documentation does not define the lifecycle;
- ask AI to decide whether servicing/setup diagnostic evidence is safe to remove.

## Revisit conditions

A future executable lane requires one of the following:

1. a Microsoft-supported exact cleanup API/command for a clearly bounded diagnostic category; or
2. a documented per-object expiration/retention signal that can be inventoried and freshly revalidated before mutation; or
3. a narrowly defined artifact for which Microsoft documents deletion as part of its normal lifecycle and whose expected disk benefit justifies a dedicated UI.

Until then, these diagnostics remain protected/report-only. The next higher-value work should move to already-audited storage families where DevClean can improve accounting and presentation without weakening mutation boundaries, especially the unified Docker UI/accounting pass.
