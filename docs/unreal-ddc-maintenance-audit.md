# Unreal Engine DDC maintenance audit

Audited: 2026-08-18

## Product conclusion

Unreal Engine Derived Data Cache (DDC) is a major disk-space target, but the correct universal action is **vendor-owned garbage collection**, not recursive deletion of folders whose names look like caches.

DevClean therefore:

- inventories known engine-local Filesystem DDC and local Zen storage locations;
- never grants raw recursive deletion authority to those roots;
- runs Unreal's `DDCCleanup` commandlet through an exact discovered `UnrealEditor-Cmd` binary;
- measures observed before/after size for the locations DevClean can identify;
- uses 2 GiB only as a benefit threshold for recommending maintenance.

No AI is needed for this lane. Unreal itself knows which cache-store entries are stale and owns the maintenance operation.

## Why DDC itself is disposable

Epic documents DDC as derived data that Unreal can regenerate from source assets. Epic explicitly advises against backing up or transferring a full DDC because regenerating it locally can be faster than restoring it.

For Filesystem DDC, Unreal's configuration exposes `DeleteUnused`, `UnusedFileAge`, `FoldersToClean`, `Clean`, and `Flush` controls. The normal safe maintenance lane is stale-data cleanup rather than an unconditional flush.

## UE 5.4+ Zen boundary

Current Epic documentation says UE 5.4 and newer use Unreal Zen Store DDC as the default local DDC. This changes the filesystem safety boundary.

Zenserver's data directory can hold **both local DDC and cooked output**. Cooked output is reference-managed, and Zenserver periodically garbage-collects unreferenced data. Therefore a disk cleaner must not recursively delete the Zen data directory just because it contains DDC data.

DevClean records Zen storage size for visibility, but `raw_delete_allowed` is always false. Mutation stays inside Unreal/Zen maintenance.

## DDCCleanup commandlet boundary

Epic's current C++ API documents `UDDCCleanupCommandlet` as an Unreal commandlet. The `ICacheStoreMaintainer` API explicitly identifies the DDCCleanup commandlet as a consumer that boosts maintenance priority and waits for registered cache-store maintenance to complete. Epic also documents that `-Run=<CommandletName>` selects the commandlet and that the `Commandlet` suffix may be omitted when resolving a commandlet name.

DevClean therefore invokes:

```text
UnrealEditor-Cmd.exe -run=DDCCleanup -unattended -NoShaderCompile -NullRHI -NoSplash -stdout -FullStdOutLogOutput
```

`-NoShaderCompile` is used because Epic's `UCommandlet` documentation explicitly recommends it when shader compilation is unnecessary for a commandlet. The command is rejected if an Unreal Editor/build process is already active.

## Discovery boundary

DevClean discovers installed engines only from strong local evidence:

1. an explicit `DEVCLEAN_UNREAL_EDITOR_CMD` override;
2. explicit `DEVCLEAN_UNREAL_ENGINE_ROOTS` roots;
3. the standard `%ProgramFiles%\Epic Games\UE_*\Engine\Binaries\Win64\UnrealEditor-Cmd.exe` layout;
4. `UnrealEditor-Cmd` on `PATH` during normal runtime discovery.

A selected executable must exactly match the currently re-discovered set before maintenance can run.

Known storage inventory includes:

- `<EngineRoot>\Engine\DerivedDataCache`;
- `%LOCALAPPDATA%\UnrealEngine\Common\Zen\Data`;
- `UE-ZenDataPath` when configured;
- `UE-LocalDataCachePath` when configured and not disabled with `None`.

`UE-SharedDataCachePath` is deliberately not inventoried or mutated by this maintenance action. Shared DDC may belong to a team or network service rather than the local user's disk-cleanup scope.

## Safety properties

- no direct `rmtree`, unlink, or raw DDC deletion;
- no deletion of DDC Pak files by filename heuristic;
- no mutation of shared/network DDC;
- no raw deletion of Zen data;
- fail closed while Unreal Editor, ShaderCompileWorker, UnrealBuildTool, or AutomationTool activity is detected;
- vendor failures are surfaced without a filesystem fallback;
- displayed Zen/known-store size is not claimed to be fully reclaimable because Zen may also contain cooked output.

## Sources

- Epic Games, **Using Derived Data Cache in Unreal Engine**: DDC architecture, local Filesystem/Zen behavior, regeneration, configuration, shared DDC boundary, and DDC Pak behavior.
- Epic Games, **Using Zen Storage Server as Cooked Output Store**: Zen data can include local DDC and cooked output; Zen periodically garbage-collects unreferenced data.
- Epic Games C++ API, **ICacheStoreMaintainer**: cache stores register for maintenance; DDCCleanup is an example consumer that boosts and waits for maintenance.
- Epic Games C++ API, **UDDCCleanupCommandlet** and **UCommandlet**: commandlet class and commandlet naming/execution behavior.
- Epic Games, **Command-Line Arguments Reference**: `NODDCCLEANUP` disables deletion of unused DDC, reinforcing that unused-DDC deletion is an engine-owned maintenance operation.
