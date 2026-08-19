# Windows Delivery Optimization exact cache maintenance audit

Last updated: 2026-08-20

## Product conclusion

Windows Delivery Optimization (DO) is a vendor-owned download/peer cache used by Microsoft content delivery. DevClean should never delete its backing directories or stop/reset the service as a disk-cleanup shortcut.

Current Windows exposes a much narrower supported lifecycle:

- `Get-DeliveryOptimizationStatus` reports exact per-file cache identity and state, including `FileId`, `FileSize`, `FileSizeInCache`, `Status`, `Priority`, `ExpireOn`, `IsPinned`, and caller information.
- `Delete-DeliveryOptimizationCache -FileID <id> -Force` deletes one exact cache item.
- `-IncludePinnedFiles` broadens deletion to pinned content and is deliberately excluded.

This is enough for a source-backed exact cache lane.

### Decision classes

For one exact status item:

- **Pinned**: REPORT_ONLY / protected.
- **Downloading / Complete / Paused**: REPORT_ONLY because the item is not in the stable retained-cache state used by this lane.
- **Unknown/future status**: REPORT_ONLY / fail closed.
- **Caching + zero cached bytes**: report-only low benefit.
- **Caching + unpinned + `ExpireOn <= now` + cached bytes > 0**: **DETERMINISTIC_CANDIDATE**. Windows itself has reached the expiration time for this unpinned cache item.
- **Caching + unpinned + future/no `ExpireOn` + cached bytes > 0**: **USER_REVIEW**. The content is reproducible/downloadable, but deleting it early trades disk space for local/peer cache value and can increase future network traffic.

The lane is exact FileId maintenance. It is not an automatic "clear Delivery Optimization" button.

## Primary Microsoft contract

Current Microsoft documentation establishes:

- Delivery Optimization caches content locally and automatically clears expired cache content according to its configured cache policy.
- Microsoft documents `DOMaxCacheAge` as the maximum time files are retained in the cache; current Windows policy ranges up to 30 days and the default is 259200 seconds (three days) in current policy documentation.
- `Get-DeliveryOptimizationStatus` exposes real-time file/job status including `FileId`, `FileSize`, `FileSizeInCache`, `Status`, `Priority`, `ExpireOn`, `IsPinned`, and caller fields.
- `Set-DeliveryOptimizationStatus` can manipulate `Pin` and `ExpireOn`, confirming those are vendor-owned lifecycle fields rather than DevClean heuristics.
- `Delete-DeliveryOptimizationCache` exposes `-FileID`, `-Force`, and `-IncludePinnedFiles`.
- Microsoft's own Delivery Optimization test procedure runs cache clearing from an Administrator PowerShell console.

Primary sources:

- https://learn.microsoft.com/windows/deployment/do/delivery-optimization-monitor
- https://learn.microsoft.com/windows/deployment/do/delivery-optimization-reference
- https://learn.microsoft.com/powershell/module/deliveryoptimization/get-deliveryoptimizationstatus
- https://learn.microsoft.com/powershell/module/deliveryoptimization/set-deliveryoptimizationstatus
- https://learn.microsoft.com/powershell/module/deliveryoptimization/delete-deliveryoptimizationcache
- https://learn.microsoft.com/windows/deployment/do/delivery-optimization-test

## Exact Windows module boundary

DevClean does not invoke whichever `powershell` or `DeliveryOptimization` module happens to appear first on PATH/PSModulePath.

The executable lane binds:

1. exact `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`;
2. exact `%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules\DeliveryOptimization\DeliveryOptimization.psd1`;
3. both as ordinary non-reparse files on local fixed storage with stable Windows file identities;
4. exact module import with `Import-Module -LiteralPath <system module manifest>`;
5. `-NoProfile -NonInteractive` PowerShell execution.

The PowerShell executable and module manifest identities are captured before and after inventory and are carried into the user's reviewed object. Any identity change before mutation fails closed.

No profile, PATH module discovery, registry profile or downloaded script contributes mutation authority.

## Inventory contract

The inventory script projects only a fixed status subset into JSON:

- `FileId`
- `FileSize`
- `FileSizeInCache`
- `Status`
- `Priority`
- `ExpireOn`, normalized to UTC ISO-8601 by PowerShell
- `IsPinned`
- `PredefinedCallerApplication`

DevClean requires:

- non-empty FileId;
- nonnegative file/cache sizes;
- boolean `IsPinned`;
- timezone-bearing parsable `ExpireOn` when present;
- unique FileIds;
- one of the explicitly audited statuses before mutation.

Unexpected or ambiguous vendor output fails closed. There is no textual/localized-table parsing.

## Why `Caching` is the only executable status

Microsoft documents several real-time DO states. The product does not need to infer what a download is doing from file timestamps or backing files.

This lane deliberately treats only `Caching` as a stable retained-cache state. `Downloading`, `Complete`, `Paused`, and any future/unrecognized state are protected. This avoids deleting a file while it is being acquired, transitioning from acquisition into cache, paused for a caller, or otherwise in a state whose destructive semantics DevClean has not audited.

## Pinned content

Pinned content is always protected. DevClean never:

- calls `Set-DeliveryOptimizationStatus -Pin $false`;
- changes `ExpireOn`;
- passes `-IncludePinnedFiles`;
- treats a large/old pinned object as reclaimable merely because the user wants disk space.

Pinning is explicit vendor state and overrides age/size benefit heuristics.

## Mutation

The only destructive action is logically equivalent to:

```powershell
Delete-DeliveryOptimizationCache -FileID <exact-file-id> -Force
```

The implementation passes the FileId through a process environment value into a fixed PowerShell script; it is never interpolated into executable PowerShell source. The script imports the exact system DeliveryOptimization module by literal path.

`-Force` is used only to suppress the vendor confirmation prompt after DevClean's own explicit user confirmation. It does not widen object scope. `-IncludePinnedFiles` is never present.

Before mutation DevClean performs two fresh inventories and requires the reviewed FileId's file size, cached size, status, priority, expiry, pin state and caller to remain unchanged. The PowerShell/module identities must also remain unchanged.

After deletion DevClean inventories again and requires the exact FileId to be absent before reporting success.

## Elevation boundary

Inventory may be shown without elevation, but mutation requires the current DevClean process to already be elevated. DevClean does not:

- invoke `runas`;
- trigger UAC itself;
- create scheduled tasks/services;
- stop the Delivery Optimization service;
- change DO policies to make content reclaimable.

This keeps privilege acquisition outside the cleanup decision and makes the UI explicit when a candidate is read-only because the current process is not Administrator.

## Reclaim accounting

`FileSizeInCache` is vendor logical cache accounting. DevClean sums it for review context and compares before/after inventory after exact deletion.

The resulting delta is evidence about Delivery Optimization's reported cache contents. It is not guaranteed to equal an immediate physical free-space increase because filesystem allocation, deduplication/compression, concurrent Delivery Optimization activity and reacquisition can differ from logical object bytes.

## Deliberate exclusions

This audit grants no authority to:

- raw deletion under Delivery Optimization service/profile/cache directories;
- whole-cache `Delete-DeliveryOptimizationCache -Force` without `-FileID`;
- `-IncludePinnedFiles`;
- changing `Pin` or `ExpireOn` to manufacture eligibility;
- stopping/restarting DoSvc;
- deleting Delivery Optimization logs as a generic shortcut;
- editing Delivery Optimization Group Policy/MDM/registry cache settings;
- using AI to decide whether a pinned/active/unknown item is safe.

## Test requirements

Regression coverage must prove at least:

- expired Caching/unpinned classification;
- future/no-expiry USER_REVIEW classification;
- pinned protection;
- active and unknown status protection;
- non-elevated read-only behavior;
- duplicate FileId fail-closed behavior;
- PowerShell/module identity race refusal;
- exact FileId carried to mutation without source interpolation;
- no `IncludePinnedFiles`;
- reviewed tool and status/pin/expiry change refusal;
- exact FileId absence postcondition.

Normal DevClean validation remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact, and CodeQL before merge.
