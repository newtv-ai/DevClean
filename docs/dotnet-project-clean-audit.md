# .NET / MSBuild project clean audit

## Decision

**Audit complete; generic executable lane deferred.**

DevClean must not treat `dotnet clean` as a universally bounded deletion of only
`bin` and `obj`, and it must not infer cleanup authority from directory names such
as `bin`, `obj`, `artifacts`, or from the presence of a `.csproj` / `.sln` file
alone.

The .NET CLI documents `dotnet clean` as an MSBuild target. That means running the
command evaluates project build logic. MSBuild explicitly supports extending the
build with custom targets that run before or after predefined targets such as
`CoreClean`, and those targets can run deletion tasks against project-defined
paths.

This is a known execution-scope problem, not an AI-review problem.

## Primary vendor contracts

Current Microsoft documentation states:

- `dotnet clean` cleans prior build output and is implemented as an MSBuild target,
  so the project is evaluated when it runs;
- ordinary SDK output includes intermediate `obj` and final `bin` content;
- MSBuild targets are project-defined execution units and can be invoked directly;
- build authors can extend predefined targets with `BeforeTargets` / `AfterTargets`
  and dependency properties;
- Microsoft's own customization example defines `CustomClean` with
  `BeforeTargets="CoreClean"` and deletes a project-defined output location;
- MSBuild's `RemoveDir` task can recursively remove a directory supplied by build
  properties.

Primary sources:

- https://learn.microsoft.com/dotnet/core/tools/dotnet-clean
- https://learn.microsoft.com/visualstudio/msbuild/how-to-extend-the-visual-studio-build-process
- https://learn.microsoft.com/visualstudio/msbuild/how-to-clean-a-build
- https://learn.microsoft.com/visualstudio/msbuild/msbuild-targets

## Why `dotnet clean` is not a generic bounded authority

The normal SDK case is attractive: `dotnet clean` is vendor-supported and is
usually understood as clearing build outputs. However, DevClean's authorization
question is stricter than whether the command is conventional.

A project may participate in clean through evaluated MSBuild logic. For example,
a custom target can run before `CoreClean` and use `Delete`, `RemoveDir`, `Exec`,
or another task against a path computed from project properties. Such a path can
be outside the conventional `bin` / `obj` directories and can be shared with
other projects or persistent user state.

Therefore proving all of the following is still insufficient to authorize a
generic clean:

1. the selected `.csproj` / `.fsproj` / `.vbproj` is real;
2. the project directory is on local fixed storage;
3. the ordinary `bin` and `obj` directories are local;
4. the user explicitly asked to reclaim project build output.

Those facts do not prove the complete effective `Clean` target graph or every
path/action reachable from it.

## Raw directory deletion is not a substitute

DevClean must not bypass this problem by recursively deleting every directory
named `bin` or `obj`.

Directory names are not semantic authority. Projects can change output paths,
share output trees, place persistent generated assets under conventionally named
directories, or use centralized artifact layouts. Conversely, a valid build can
write elsewhere through evaluated MSBuild properties.

A future implementation may only act on exact paths that are proven from the
specific evaluated project configuration and whose lifecycle is independently
safe.

## Solution and multi-project scope

A solution-level `dotnet clean` can evaluate and clean multiple projects. That
widens the authority problem: each project can import different targets and
properties and can define its own clean extensions.

DevClean therefore must not assume that selecting one `.sln` or `.slnx` file
creates one bounded cleanup root.

## USER_REVIEW / AI classification

This lane should not be sent to AI merely because its destructive scope is not
proven. The semantics are technically understood: MSBuild allows project-defined
clean behavior, so generic authority is missing.

Until a complete scope proof exists, the generic .NET project-clean action is
**REPORT_ONLY / deferred**, not `AI_REVIEW`.

If a future adapter can prove a narrow exact output set that is purely generated
and reproducible, that narrower set can be classified independently.

## Revisit condition

An executable .NET/MSBuild project lane needs a stable, complete pre-mutation
model of the exact effective clean behavior for the evaluated project or solution.
At minimum it must:

1. bind an exact project/solution identity and exact SDK/MSBuild toolchain;
2. evaluate the same configuration/framework/runtime/property set that would be
   cleaned;
3. enumerate every effective clean target/action that can mutate storage,
   including imported and `BeforeTargets` / `AfterTargets` extensions;
4. resolve every affected path before execution and reject unresolved/dynamic
   destructive actions such as arbitrary `Exec` cleanup;
5. require every mutable path to fall within independently approved local-storage
   boundaries;
6. re-evaluate the complete scope immediately before mutation and require an
   identity match;
7. use the vendor clean target only after that proof, with no raw-delete fallback;
8. verify postconditions without assuming that logical project cleanup implies a
   fixed amount of reclaimed bytes.

If MSBuild does not expose a stable, complete machine-readable destructive
manifest for this purpose, DevClean should continue to defer generic execution.

## Deliberate exclusions

This audit grants no authority to:

- recursively delete `bin`, `obj`, `artifacts`, `TestResults`, or similarly named
  directories;
- run `dotnet clean` or `msbuild /t:Clean` generically;
- clean a whole solution because its file is on local fixed storage;
- execute project-defined cleanup commands through `Exec`;
- modify NuGet package caches (covered by the separate NuGet lane);
- remove published applications, deployment bundles, user secrets, signing
  material, SDKs, workloads, or installed tools;
- use AI to invent a clean command or guess which generated paths are expendable.
