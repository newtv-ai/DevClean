# Platform storage family reconciliation — 2026-08

## Scope

This current-main reconciliation closes four second-pass family rows whose product-specific source audits and executable/report-only boundaries already landed with regression coverage. It does not create new cleanup authority or reinterpret generic names, ages, sizes, or apparent regenerability as deletion evidence.

## Docker / Podman / WSL

The current tree already separates these storage systems by exact vendor identity and locality instead of raw Docker Desktop/Podman/WSL filesystem paths.

- Docker: #81 defined the object split; #84 bound mutations to a proven local daemon; #90, #92, #97, #101 and #130 landed Buildx cache, exact image, exact stopped-container, read-only volume and unified maintenance behavior. Volumes remain persistent/report-only, and `docker system prune` / raw Desktop WSL-VHD mutation remain excluded.
- Podman: #121 and #122 bind stopped-container and exact image actions to one proven Podman-managed local Windows machine connection; #123 leaves persistent build cache and all volumes report-only because current prune surfaces are broader or the objects can contain unique data.
- WSL: #88, #91, #95, #96, #102, #106, #103/#104/#109/#111/#112 establish the persistent-distro/VHD ceiling, reject unsafe sparse conversion, constrain in-distro maintenance to exact non-shell argv and distro-root-filesystem paths, and delegate pip/uv/pnpm/Go cleanup only to each tool's audited vendor lifecycle. No unregister, raw VHD mutation, generic Linux cache deletion or host-space reclaim promise is introduced.

Conclusion: the family is re-verified. Positive mutation remains exact vendor-operation scoped and locality-bound; persistent/shared/ambiguous state remains protected or report-only.

## Android SDK / AVD

#124 replaced stale package work with current-main sdkmanager package identity and a strict AVD/system-image correlation gate. An installed package can enter USER_REVIEW only from the exact SDK's own `sdkmanager` inventory; system images remain protected whenever any safely enumerated AVD references them, and incomplete AVD proof fails closed for all system-image uninstall authority. #131 adds only read-only positive project reference evidence and explicitly forbids negative "unused" inference from Gradle source.

Conclusion: the Android SDK/AVD family is re-verified. The SDK root itself remains installed developer tooling, package age/size never create deletion authority, and project text is not used as a completeness proof.

## Unity / Unreal

Unity is already split by source-owned storage semantics rather than a generic Unity-cache rule: #64 covers one selected project's exact `Library` as USER_REVIEW; #65 covers exact cached Asset Store `.unitypackage` files as USER_REVIEW; #66 keeps the active Unity 6 UPM registry database Unity-managed while exposing only deprecated `packages` as an explicit compatibility tradeoff; #67 keeps GI cache vendor-managed/protected; #68 applies the local-fixed-storage execution ceiling to direct Unity filesystem mutation.

Unreal #62 delegates local DDC maintenance to the engine-owned `DDCCleanup` commandlet and explicitly refuses raw `DerivedDataCache` / Zen-data deletion because modern Zen storage can mix DDC with cooked output.

Conclusion: the Unity/Unreal family is re-verified. Shared/configurable caches do not inherit raw deletion authority, and positive actions stay tied to exact project/package/vendor command boundaries.

## Windows diagnostics / servicing / Recycle Bin / previous install

The current Windows lanes are already source-separated and deliberately avoid broad filesystem cleanup shortcuts:

- #117: DISM component-store analysis plus USER_REVIEW `StartComponentCleanup`; no raw WinSxS/SoftwareDistribution/CBS deletion and no `/ResetBase`.
- #118: previous-install cleanup is USER_REVIEW and invokes only the documented `cleanmgr /AUTOCLEAN` upgrade-leftover lifecycle after exact `Windows.old`/tool/process revalidation.
- #119: Recycle Bin uses exact per-drive Shell APIs; no raw `$Recycle.Bin`, no all-drive NULL scope, no Downloads deletion or generic cleanmgr profile authority.
- #125: Delivery Optimization uses exact Microsoft FileId/status/pin/expiry state and the vendor cache-delete cmdlet rather than raw backing-directory deletion.
- #126/#127/#128: crash dumps and live-kernel dumps are exact source-backed USER_REVIEW diagnostic files; age and size never make them automatic, and mutation is handle-bound to exact local files.
- #129: Panther/setup/CBS/DISM/SetupDiag troubleshooting logs remain REPORT_ONLY because Microsoft documents diagnostic value but no bounded per-object expiry/removal contract.

Conclusion: the Windows family is re-verified. Servicing, rollback, diagnostic and recoverable-user-data semantics remain distinct, and unsupported broad cleanup stays protected/report-only.

## Cross-family invariants retained

- No generic path name, age, size or "cache-like" label creates authority.
- Learned file rules cannot override application KEEP/protected semantics and never become directory authority.
- Direct filesystem mutation remains local-fixed, identity/reparse/hardlink/concurrency guarded.
- Vendor commands are accepted only when their complete relevant scope can be bound and freshly revalidated; broad prune/system-clean shortcuts remain excluded when they mix semantic classes.
- Logical/vendor-reported bytes are evidence, not guaranteed physical host reclaim.

## Revisit triggers

Reopen a row only for a concrete upstream lifecycle/API change, a newly discovered current-main regression, or a supported vendor surface that materially changes the bounded mutation proof. Do not reopen merely to chase additional raw-delete coverage.