# WSL developer-tool execution adapter audit

Audited: 2026-08-19

## Product conclusion

The next useful WSL lane is not VHD mutation. It is a narrow execution adapter that lets DevClean invoke **already-audited vendor maintenance commands inside one exact registered WSL distribution**.

WSL provides the required process boundary: an exact distribution can be selected with `--distribution/-d`, and `--exec/-e` executes the supplied Linux command without launching the distribution's default shell. DevClean should use that argv-based form and must not build `sh -c`, `bash -c`, PowerShell, or interpolated command strings from user/tool data.

This adapter does not itself create cleanup authority. Each Linux tool lane still needs its own audited command, inventory, process guard, path/config verification, and revalidation immediately before mutation.

## 1. Exact distribution identity

Every adapter operation must begin from the read-only WSL inventory lane and retain the exact WSL-reported distribution display name.

Before a mutation, DevClean must query registered distributions again and require the selected distribution to still exist with the same case-insensitive identity. It must not silently fall back to the default distribution if the selected distro disappears or is renamed.

The Windows-side command shape is therefore structurally fixed:

```text
wsl.exe --distribution <exact-distro-name> --exec <tool> <argv...>
```

The distro name is one argv element. Tool arguments are separate argv elements. No shell quoting language is introduced by DevClean.

## 2. No generic remote shell feature

The adapter must never become a terminal or arbitrary-command runner.

Allowed commands must be selected from code-defined, reviewed tool operations. The UI may choose a distro, a known maintenance action, and bounded parameters that the specific tool contract already permits; it may not accept an arbitrary command line from the user, a model, a configuration file, or scanned filesystem text.

AI has no role in constructing commands.

## 3. Reuse semantics, not Windows path implementations

Existing native Windows maintenance modules cannot simply be called through WSL. Many of them deliberately verify Windows-local executable paths, Windows fixed-storage identity, Windows process names, or Windows-formatted tool paths. For example, the current pip lane validates the cache directory returned by `pip cache dir` against a Windows `Path`/`PureWindowsPath` target before allowing `pip cache purge`.

The WSL lane should reuse the **semantic contract** instead:

1. ask the tool inside the selected distro for its authoritative cache/store path;
2. require an absolute Linux path where the tool exposes one;
3. inventory size/read-only state inside that same distro;
4. check relevant process/activity state inside that same distro when the vendor operation can race active work;
5. re-query the authoritative path immediately before mutation;
6. invoke only the audited vendor maintenance argv;
7. inventory again and report logical Linux bytes changed.

Windows-side directory guessing must not substitute for those checks.

## 4. Initial candidate tools

The first implementation candidates should be commands that already have narrow vendor-owned cleanup semantics and do not require project deletion heuristics. Good candidates from existing DevClean audits include pip cache maintenance, uv cache garbage collection, pnpm store prune, and Go cache maintenance.

They should be added one at a time. A successful WSL adapter for pip does not automatically authorize npm, Cargo, apt, Docker, or arbitrary cache directories.

Project build output remains project-aware. Cargo/Bazel/Unity/etc. must continue to establish the exact workspace and tool-owned output semantics before cleanup, even when executed inside WSL.

## 5. Linux package managers need separate audits

`apt`, `dnf`, `pacman`, and similar package managers are not covered merely because DevClean can execute a command inside WSL. Package-manager caches, downloaded package archives, package databases, and autoremove operations have different semantics.

In particular, dependency/package removal must never be smuggled into a cache-maintenance lane. Any future package-manager support needs a separate source audit and should distinguish cache cleanup from installed-package lifecycle changes.

## 6. User and privilege boundary

By default, the adapter should execute as the distribution's configured default user. DevClean must not add `--user root`, `sudo`, `su`, privilege escalation, or password handling unless a future separately audited operation explicitly requires it.

A maintenance action that fails because the default user lacks permission should fail closed rather than escalating automatically.

This is especially important because WSL distributions may contain production-like databases, services, secrets, SSH keys, source repositories, and mounted Windows/network storage.

## 7. Working-directory boundary

Global tool-cache operations should not need a project working directory. Project-scoped operations must establish an exact Linux workspace path through the tool/project contract and pass it through WSL's supported working-directory mechanism only when needed.

DevClean must not translate a scanned Windows path to `/mnt/<drive>/...` by string substitution and then assume ownership. WSL automount configuration can differ, and Linux paths may refer to distro storage, mounted Windows volumes, network filesystems, bind mounts, or other shared state.

Unknown/shared/mounted storage should remain report-only until ownership and locality can be proven for that tool lane.

## 8. Process and race guards

Windows process guards do not prove that a tool is idle inside WSL. Each WSL tool lane must define its own safe activity check.

Where a vendor command is itself concurrency-safe and documented to handle active readers/writers, the audit may rely on that vendor behavior. Otherwise the lane needs a distro-local guard or must stay USER_REVIEW/REPORT_ONLY.

Regardless of the process guard, authoritative tool paths/configuration and exact distro identity must be rechecked immediately before mutation.

## 9. Accounting: logical Linux bytes, not promised Windows reclaim

An in-distro cleanup frees logical filesystem space. It does not prove that the same number of bytes are immediately returned to the Windows host because WSL 2 commonly stores the filesystem in a VHD.

The adapter should report:

- logical bytes before/after for the tool-owned Linux cache/store where measurement is reliable;
- the vendor command actually executed;
- the exact distro identity;
- no claim that the same number of NTFS bytes were physically reclaimed.

Physical host-space reclamation remains the separate VHD lane and is currently not executable in DevClean.

## 10. Failure model

The adapter must fail closed if any of these occur:

- selected distro no longer exists;
- WSL cannot confirm distro identity/state;
- tool executable is unavailable;
- authoritative cache/store path cannot be obtained or changes before execution;
- path is relative/ambiguous when the tool contract requires an absolute target;
- the tool reports an error;
- required activity/ownership checks cannot be established;
- post-operation inventory cannot be confirmed.

A failed WSL call must never trigger Windows-side path deletion as a fallback.

## Proposed implementation order

### Phase A — generic non-shell runner + exact distro guard

Implement a small internal adapter that accepts only a code-supplied executable plus argv, pins one exact registered distribution, captures byte-safe stdout/stderr, applies timeouts, and revalidates distro identity. It grants no standalone UI or arbitrary command surface.

### Phase B — first audited tool: pip cache

Port the existing pip semantic boundary to Linux paths: obtain `pip cache dir` inside the selected distro, inventory it, refuse ambiguous state, and invoke only `pip cache purge` for that freshly revalidated tool-owned cache.

### Phase C — uv / pnpm / Go

Add one tool at a time, preserving each existing vendor command and its own safety checks rather than building a generic `cache` directory cleaner.

### Phase D — project-aware tools

Only after the generic WSL execution boundary is proven should Cargo/Bazel/etc. be adapted, still requiring exact project/tool metadata and never generic directory-name deletion.

## Explicit non-targets

This audit grants no authority to:

- execute arbitrary user/model-provided Linux command strings;
- invoke `sh -c`, `bash -c`, or another shell to interpret dynamically constructed text;
- add `sudo`, `su`, or `--user root` automatically;
- scan `/home`, `/tmp`, `.cache`, `build`, `target`, or similar names and delete them generically;
- translate Windows paths to `/mnt/...` and assume they are the same storage object;
- clean Docker Desktop's WSL distributions through this adapter;
- mutate WSL VHDs, unregister distros, or promise physical Windows reclaim from logical Linux deletion.

## Primary sources

- Microsoft Learn, **Basic commands for WSL** — exact distribution selection and WSL status/version surfaces.
- microsoft/WSL current command-line help/source — `--exec/-e` executes a command without the default Linux shell; `--distribution/-d` selects the target distribution.
- Existing DevClean vendor-maintenance modules — semantic cleanup contracts that must be reimplemented with Linux-native identity/path checks rather than Windows path assumptions.
