# WSL storage and VHD maintenance audit

Audited: 2026-08-19

## Product conclusion

WSL storage must not be represented by a generic `ext4.vhdx` cleanup rule. A WSL distribution's virtual disk contains the distribution's actual Linux filesystem and user data, so the virtual disk itself is persistent state rather than cache.

The WSL lane should be split by semantics:

| Storage / operation | DevClean conclusion | Reason |
| --- | --- | --- |
| Distribution virtual disk / Linux root filesystem | KEEP / REPORT_ONLY | Contains installed packages, projects, databases, home directories and other persistent user state |
| `wsl --unregister <Distribution>` | protected destructive administration | Microsoft documents that unregister permanently deletes that distribution's data, settings and software; this is not disk cleanup |
| `wsl --export` | backup / migration utility, not cleanup | Useful as a safety primitive, but does not itself prove that deletion is appropriate |
| Distribution-internal package/tool caches | audit separately inside the distribution | Must use the Linux package/tool's own semantics rather than deleting Windows-side VHD files |
| Distribution-internal project build output | audit separately by project/tool | Same project-aware rules as native builds; do not infer disposability from Linux path names |
| VHD free-space reclamation / sparse behavior | WSL-managed maintenance candidate | Physical host-space reclamation is distinct from deleting Linux files and should use supported WSL/VHD mechanisms only |
| Raw `ext4.vhdx` deletion, truncation or arbitrary host-side file mutation | protected | Can destroy the entire distribution or corrupt its filesystem |

Known WSL semantics do not need AI. The main distinction is between persistent distro state, vendor-managed physical-space reclamation, and Linux-internal cleanup that belongs to the corresponding package/build tool.

## 1. Distribution identity must come from WSL

Current Microsoft documentation exposes distribution identity through WSL itself, including commands such as:

- `wsl --list --verbose` / `wsl -l -v`;
- `wsl --list --running`;
- `wsl --terminate <Distribution>`;
- `wsl --shutdown`.

DevClean must start from WSL's exact distribution name/state rather than searching user-profile or package directories for files named `ext4.vhdx`.

That matters because WSL distributions can be installed/imported in different ways and their storage location is not safely inferable from a generic path pattern. A path that happens to contain an `ext4.vhdx` is not sufficient deletion or compaction authority.

## 2. `wsl --unregister` is explicitly not cleanup

Microsoft's WSL command documentation warns that `wsl --unregister <DistributionName>` removes the distribution from WSL and permanently deletes all data, settings and software associated with it.

DevClean must therefore never present unregister as a space-saving cleanup action, regardless of age or size.

If the product later offers distribution lifecycle management, it must be a separate administrative workflow with explicit export/backup choices and an unmistakable data-loss warning. It does not belong to the normal cleanup recommendation lane.

## 3. Export/import is a safety and migration primitive

WSL provides vendor commands to export and import distributions. Export can create a backup/archive and import can restore or relocate a distribution.

These operations are useful for future workflows such as:

- user-requested migration to another drive;
- backup before deliberate distribution removal;
- controlled recreation/relocation.

They are not evidence that a distribution should be deleted and should not be invoked automatically merely because the VHD is large.

## 4. Physical VHD space and logical Linux free space are different

A WSL 2 distribution stores its Linux filesystem in a virtual hard disk. Deleting files inside Linux can create free blocks within the filesystem without immediately guaranteeing the same amount of host NTFS space is returned.

This means DevClean must report two different concepts when possible:

1. **logical distribution usage/free space** inside the Linux filesystem;
2. **physical Windows host size** of the distribution's virtual disk.

A package-cache cleanup inside Linux may free logical filesystem space while host `ext4.vhdx` size remains similar until the supported VHD/sparse mechanism reclaims blocks.

DevClean must not promise that logical bytes deleted inside WSL equal physical Windows bytes reclaimed.

## 5. Sparse VHD behavior is vendor-managed

Current WSL documentation exposes sparse-VHD behavior through supported WSL configuration/management surfaces on current versions. This is fundamentally different from DevClean opening the VHD file and mutating it directly.

The safe product direction is:

- detect WSL version/capabilities through WSL itself;
- inventory distribution state read-only;
- if a supported WSL command can enable/manage sparse behavior for an exact distribution, treat that as a vendor-owned storage-policy action;
- never edit undocumented WSL registry/package state to discover or manipulate a disk;
- never raw-truncate or directly compact a mounted/running distribution VHD;
- fail closed on older WSL versions where the supported operation is unavailable or ambiguous.

Sparse mode can change how future unused blocks are returned to Windows, but it does not justify deleting Linux files. It is a storage-policy choice, not a filesystem-cleanup classifier.

## 6. Offline VHD compaction must remain separate

Microsoft's WSL disk-space guidance describes host-side VHD maintenance only when the virtual disk is not actively in use. A future compaction lane therefore needs a stricter contract than ordinary cleanup:

1. identify one exact WSL distribution through WSL;
2. establish an authoritative source-backed mapping to its exact virtual disk before any host-side action;
3. stop/terminate the distribution, or use `wsl --shutdown` where the vendor procedure requires it;
4. prove the VHD is no longer mounted/in use;
5. invoke only a documented Windows/WSL VHD compaction mechanism;
6. never modify filesystem contents directly from the host;
7. re-check distribution registration and disk identity before and after;
8. report observed physical host-size change, which may be zero;
9. never combine compaction with unregister/delete.

Until the exact distro-to-VHD mapping and supported compaction flow can be proven source-backed for current WSL variants, DevClean should **not** expose a raw compaction button.

This is execution-authority uncertainty, not AI work.

## 7. Distribution-internal cleanup belongs to Linux tools

A WSL distribution can contain the same large developer storage as a native Linux machine: apt/dnf/pacman package caches, pip/uv/npm/pnpm caches, Cargo target directories, Go caches, Docker/Podman state, model stores and project build output.

DevClean should not solve that by mounting/traversing the distro filesystem from Windows and applying directory-name rules.

The correct long-term architecture is a WSL execution adapter:

- enumerate an exact registered distribution through WSL;
- invoke the relevant vendor/tool command *inside that distribution*;
- reuse DevClean's semantic policy where the vendor semantics are equivalent;
- keep Linux paths scoped to the exact distro and tool configuration;
- refuse unknown/shared/mounted storage where ownership cannot be established;
- report Windows host reclaim separately from logical Linux cleanup.

Examples of the product principle:

- apt package cache → apt-owned command if audited;
- pip/uv/pnpm/Go → their own audited cleanup commands, executed inside WSL;
- Cargo/Bazel/project output → exact project-aware tool commands;
- arbitrary `/tmp`, `cache`, `build`, `.local`, home-directory names → no generic deletion authority.

## 8. WSL distro runtime state

Before any future distribution-level storage mutation, DevClean should use WSL's own running-state commands and fail closed if the target cannot be safely brought offline when required.

`wsl --terminate <Distribution>` and `wsl --shutdown` are lifecycle controls, not cleanup operations. DevClean should never terminate a user's running development environment merely to save a small amount of space without an explicit operation that requires it and clear user confirmation.

## Proposed implementation order

### Lane A — read-only WSL inventory

First implementation should be non-destructive:

1. locate the configured/system `wsl.exe`;
2. obtain WSL version/capability information;
3. enumerate exact registered distributions and running state through WSL;
4. report each distribution as persistent state;
5. optionally query logical filesystem usage inside the distribution through a read-only Linux command;
6. do not guess VHD paths or grant any raw deletion authority.

Decision: **REPORT_ONLY**.

### Lane B — source-backed sparse/VHD policy

After current WSL command support is pinned and tested:

- expose only vendor-supported per-distribution sparse/storage-policy operations;
- keep it USER_REVIEW if the behavior changes performance/fragmentation tradeoffs;
- no AI;
- no raw VHD mutation.

### Lane C — WSL tool adapter

Reuse already-audited developer-tool cleanup logic inside a selected distribution, beginning with high-confidence vendor commands. This can create significant logical free space while retaining the same deterministic/user-review policy used on Windows.

### Lane D — physical compaction

Only after an authoritative distro-to-VHD identity and offline compaction flow is source-backed across supported WSL installation/import variants.

Until then: report-only.

## Explicit non-targets

This audit grants no authority to:

- raw-delete `ext4.vhdx`, `*.vhdx` or distribution package folders;
- call `wsl --unregister` for cleanup;
- delete Linux `/home`, projects, databases or package state;
- delete generic Linux directories named `cache`, `tmp`, `build`, `target` or similar;
- manipulate WSL registry entries or Store package metadata using undocumented state;
- mutate Docker Desktop's WSL disks through the WSL lane;
- compact a running/mounted distribution disk;
- treat a distro's logical free space as immediately reclaimed Windows disk space.

## Primary sources

- Microsoft Learn, **Basic commands for WSL**
- Microsoft Learn, **How to manage WSL disk space**
- Microsoft Learn, **Advanced settings configuration in WSL** (`.wslconfig` / sparse VHD behavior where supported)
- Microsoft Learn, **Import any Linux distribution to use with WSL**
