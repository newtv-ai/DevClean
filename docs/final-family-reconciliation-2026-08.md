# Final application and build-family reconciliation — 2026-08

## Scope

This current-main reconciliation closes the final four second-pass tracker rows. All product-specific source audits, positive maintenance lanes, negative/report-only decisions and regression tests cited below already landed. This PR does not create new deletion authority or reinterpret generic names, age, size, redownloadability or AI/user guesses as lifecycle proof.

## .NET / NuGet

- NuGet #33 inventories the exact effective `global-packages`, `http-cache`, `temp` and `plugins-cache` resources and grants no raw recursive authority; all mutation is delegated to exact-root-confirmed `dotnet nuget locals`.
- #55 keeps HTTP/temp/plugin cache deterministic maintenance but treats `global-packages` as USER_REVIEW because PackageReference projects consume that store directly and clearing it requires restore.
- .NET workload #51 exposes only conservative vendor `dotnet workload clean`, never the aggressive `--all` mode or guessed workload-pack deletion.
- .NET global tools #54 are installed user software and therefore USER_REVIEW through exact `dotnet tool list/uninstall --global`, not generic file cleanup.
- .NET/MSBuild project clean #114 is explicitly REPORT_ONLY/deferred because MSBuild project/import-defined target graphs can widen `Clean` beyond conventional `bin`/`obj` paths.

Conclusion: the .NET/NuGet family is re-verified. Exact vendor local-resource/installed-tool identities are used where scope is provable; dependency stores, SDK/workload state and project-defined clean graphs do not gain raw filesystem authority.

## Go / Cargo / Conan / vcpkg

- Go #34 discovers exact effective build/module caches and protects project metadata/tooling; #59 separates deterministic build-cache cleanup from USER_REVIEW module-cache intent and routes both through `go clean` with exact-root confirmation. The WSL follow-ups (#107/#112) preserve the same semantic split under exact distro/root-filesystem bounds.
- Cargo #35 keeps global registry/git caches report-only because Cargo owns automatic GC and its home also contains binaries/config/credentials/install metadata; #69 grants USER_REVIEW only to an exact Cargo-reported project target directory inside the selected local workspace; #99 keeps the separately configured build directory Cargo-managed because stable Cargo metadata does not expose its effective path.
- Conan 2 #61 delegates only vendor `conan cache clean "*"` generated-cache cleanup after exact home/version/process revalidation; package artifacts, recipes, config, remotes and credentials remain outside the lane.
- vcpkg #77 inventories exact root-local `packages`, `buildtrees` and `downloads` as USER_REVIEW, warns about editable buildtree source, keeps binary cache report-only, and never touches installed packages/manifests/ports/triplets/registries/integration state.

Conclusion: the family is re-verified. Positive maintenance is tool-owned and exact-scope; shared dependency stores, editable/project state, credentials/config and ambiguous/custom storage remain protected or explicit user tradeoffs.

## Ollama / LM Studio and local-model products

- Ollama #38 classifies downloaded models as USER-owned content, protects `.ollama` configuration/unknown state and grants zero raw model-store deletion authority because blobs/manifests can be shared.
- #72 adds the narrow positive lane: exact model identity from the loopback Ollama API, loaded-model refusal, local-fixed model-root proof, fresh name/digest/running-state revalidation and only official model DELETE API mutation. Raw blob/manifest/model-root deletion remains unavailable.
- LM Studio was corrected in the built-in authority audit (#53): its model store contains downloaded/imported user-selected models and therefore does not qualify for deterministic whole-tree cleanup. The packaged root remains discovery/report-only after the cross-cutting phase-2/3 authority changes.
- Other local-model directories receive no authority merely from `models`, `blobs`, `weights`, cache-like naming, age or size. Without an exact product-owned identity/delete lifecycle they stay protected/report-only.

Conclusion: the local-model family is re-verified. Model data is user-selected content; only an exact vendor model object with a bounded local mutation API can enter a positive deletion lane.

## Project build systems

- Bazel #63 binds workspace/output-base identity through Bazel itself and uses vendor `clean`; `--expunge` is USER_REVIEW. #70 keeps redirectable/shared Bazel disk cache report-only and leaves GC to Bazel unless a stable effective configuration/control interface exists.
- Cargo project targets are covered by #69; generic `target` names do not grant authority and external/shared target directories stay non-executable.
- CMake #113 is audit-complete REPORT_ONLY/deferred: configured clean can include project-defined/global additional clean files, generated/byproduct paths and generator-specific behavior, so neither `build` directory names nor vendor clean are a complete bounded scope proof.
- .NET/MSBuild #114 is likewise REPORT_ONLY/deferred because evaluated Clean target graphs can execute project/import-defined destructive behavior outside conventional output directories.
- Meson #115 is the narrow positive exception: exact configured out-of-source build-directory removal is USER_REVIEW after Meson introspection binds the selected source/build pair and all local-fixed/identity/process guards pass.
- Ninja, GNU Make/Automake and SCons #116 are audit-complete REPORT_ONLY/deferred: clean behavior can escape the local graph through dyndeps/multi-output edges, arbitrary Make recipes/extensions, or executable SCons Python/configuration. No dry-run or human-text parsing becomes mutation authority.

Conclusion: the build-system family is re-verified. Project code/configuration is never executed merely to manufacture cleanup scope, generic output-directory names grant no authority, and the only positive project lanes are those whose complete relevant vendor scope can be independently bounded and freshly revalidated.

## Final invariants

- Generic path/name/age/size/rebuildability never create deletion authority.
- Reusable learned rules remain file-only and cannot override application KEEP/USER ceilings or create directory authority.
- Direct filesystem mutation remains local-fixed and exact identity/reparse/hardlink/concurrency guarded.
- Vendor commands/APIs are accepted only when the complete relevant scope is known, exact identities are bound, and state is freshly revalidated.
- Shared dependency/model/cache storage and project-defined execution fail closed when completeness cannot be proven.

## Revisit trigger

Reopen a completed row only for a concrete upstream API/lifecycle change or a verified current-main regression. Do not reopen merely to increase cleanup coverage.