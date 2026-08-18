# Git and Git LFS storage audit

Audited: 2026-08-19

## Product conclusion

Git repository storage and Git LFS storage must be treated as two related but independent maintenance lanes. Neither should be represented by a generic `.git` cleanup rule.

| Storage / operation | DevClean conclusion | Reason |
| --- | --- | --- |
| Git object database / refs / reflogs / worktree metadata | vendor-managed | Git owns reachability, repacking, reflog expiry, cruft handling and stale-worktree metadata |
| `git maintenance run --auto` | deterministic vendor maintenance | Git itself checks configured thresholds and runs only maintenance tasks that are currently needed |
| forced/manual `git gc` | separate follow-up, not default | can expire recovery metadata and unreachable objects according to repository configuration; more work than `--auto` |
| direct `.git/objects`, `logs`, `worktrees`, `rr-cache` deletion | protected | raw paths do not encode reachability or recovery semantics |
| `git clean`, reset-like operations, untracked files | protected | may delete user-authored working-tree data and are outside disk-cache maintenance |
| default repository-local Git LFS object storage | USER_REVIEW via `git lfs prune` | old local LFS copies can be pruned by LFS's own reachability/recent/unpushed rules, but local/offline copies can still be valuable |
| custom `lfs.storage` | REPORT_ONLY | Git LFS explicitly warns not to prune when different repositories share the same custom storage directory |
| `git lfs prune --force` | protected | deliberately discards additional normally protected local objects |

Known semantics should not consume AI. Git's maintenance engine decides its own housekeeping, while LFS pruning is a known user-value tradeoff.

## Git repository maintenance

Current Git documentation describes `git maintenance` as the vendor-owned mechanism for optimizing repository data, both to improve command performance and to reduce storage requirements.

For `git maintenance run --auto`, Git tests task-specific thresholds before doing work. With default configuration, the `gc` task is the only enabled manual/auto maintenance task. Repository configuration may select a supported maintenance strategy or enable additional documented tasks.

This is materially different from DevClean guessing that files under `.git/objects` are disposable. Git owns object reachability, refs, reflogs, cruft packs, rerere state and linked-worktree administration and must retain that authority.

### Deterministic lane

The first executable Git lane should therefore use only:

`git maintenance run --auto`

DevClean may first use `git maintenance is-needed --auto` when supported to explain whether Git currently considers maintenance necessary. A no-op is a valid result.

DevClean should not override the user's Git maintenance strategy or expiry settings. The exact repository's own configuration remains authoritative.

### Recovery-data boundary

Git reflogs record previous reference values and are useful for recovering earlier branch/HEAD states. Git's normal expiry defaults are age based, and `git gc`/maintenance owns when that state becomes eligible for expiration.

DevClean must not expose shortcuts such as:

- `git reflog expire --expire=all`;
- `git prune --expire=now`;
- raw deletion of `.git/logs`;
- raw deletion of loose/pack objects;
- direct removal of `.git/worktrees` entries.

Those operations can destroy recovery paths or violate repository relationships merely to gain space.

`git worktree prune` itself is vendor-owned and only removes administrative entries for missing worktrees, but the expected disk win is normally tiny. It should remain inside Git maintenance rather than become a headline cleanup button.

## Repository identity and path authority

The executable implementation must not assume that a repository's metadata lives at `<selected>/.git`.

Git supports bare repositories, linked worktrees, gitfiles, alternate object locations and a common Git directory. Git's own documentation specifically directs callers to `git rev-parse --git-path` rather than making layout assumptions.

For a selected working repository DevClean should ask the configured Git executable for authoritative identity using source-backed commands such as:

- `git rev-parse --show-toplevel`;
- `git rev-parse --absolute-git-dir`;
- `git rev-parse --path-format=absolute --git-common-dir`;
- `git rev-parse --git-path objects` for observed storage;
- `git count-objects -v` for vendor-reported object-database inventory.

The selected worktree root must exactly match Git's reported top level before any operation. Bare repositories should be audited separately rather than silently treated as normal worktrees.

Alternate object databases reported by `git count-objects -v` are visible shared dependencies, not DevClean deletion targets.

## Git LFS semantics

Git LFS stores pointer files in Git history and the large content separately in LFS storage. Its official `git lfs prune` command deletes old local LFS objects according to LFS-aware rules instead of using filename or age guesses.

Default prune retains objects referenced by important current/local state including:

- the current checkout;
- stashes;
- recent branches and recent commits;
- commits that have not been pushed to the configured prune remote;
- other worktree checkouts.

If no default remote can be established, LFS treats objects as unpushed rather than blindly pruning them.

The prune command also supports `--dry-run` and optional remote verification. DevClean should use these vendor features instead of traversing `.git/lfs/objects` itself.

### Why LFS pruning is USER_REVIEW

Even when a local object has a remote copy and Git LFS considers it prunable, deleting it has a user-specific cost: a later checkout may need to download the object again, and the local copy can have value on slow, metered, restricted or offline networks.

Therefore LFS pruning is not an automatic default merely because the object is reproducible. It is **USER_REVIEW**, with no AI required.

The initial executable lane should use the normal prune policy and should prefer remote verification when it can be performed without changing repository configuration. It must never add `--force`.

## Custom/shared LFS storage

Git LFS configuration explicitly allows `lfs.storage` to override the default storage directory and explicitly warns that `git lfs prune` must not be run when different repositories share the same custom storage directory.

DevClean cannot prove exclusive ownership of an arbitrary configured storage directory from a path alone. The safe rule is therefore:

- if `lfs.storage` is unset, the repository-local default can be considered for vendor pruning;
- if `lfs.storage` is configured, show the storage as REPORT_ONLY and do not run prune in the initial implementation;
- never raw-delete a configured LFS storage directory.

This intentionally gives up some reclaim opportunities in exchange for a clear ownership boundary.

## Proposed executable split

### Lane A — Git automatic maintenance

Decision: **DETERMINISTIC_CANDIDATE** when Git says maintenance is needed.

Execution contract:

1. user selects a Git worktree root;
2. resolve the configured Git executable;
3. require `rev-parse --show-toplevel` to exactly match the selected root;
4. record absolute Git/common/object paths for inventory only;
5. require repository/Git storage to remain on local fixed storage for DevClean's local maintenance surface;
6. refuse while an overlapping Git maintenance process is already active;
7. re-resolve repository identity immediately before mutation;
8. invoke only `git maintenance run --auto` inside that exact repository;
9. never fall back to raw filesystem deletion;
10. measure observed Git storage before/after and report that the vendor operation may legitimately reclaim zero bytes.

### Lane B — Git LFS prune

Decision: **USER_REVIEW**, never preselected and never sent to AI by default.

Execution contract:

1. start from the same exact validated Git repository;
2. require an installed Git LFS client and inventory its version;
3. detect whether LFS is actually configured/used before showing the lane;
4. if `lfs.storage` is explicitly configured, keep the lane report-only;
5. run `git lfs prune --dry-run` first to show that LFS itself finds candidates;
6. require explicit confirmation;
7. revalidate repository identity and LFS storage policy immediately before execution;
8. run normal `git lfs prune`, preferably with `--verify-remote` when verification is viable;
9. never use `--force` and never raw-delete `.git/lfs` objects;
10. measure local LFS storage before/after; do not claim remote storage was changed.

## Explicit non-targets

This audit grants no authority over:

- working-tree tracked or untracked files;
- ignored build outputs merely because Git ignores them;
- stash contents;
- branches, tags, remotes or refs;
- submodule working trees;
- hooks or Git configuration;
- Git credentials;
- arbitrary `.git` directories found by filesystem scanning;
- alternate/shared object databases;
- custom/shared LFS storage;
- remote Git or LFS server storage.

## Primary sources

- Git documentation, **git-maintenance**
- Git documentation, **git-gc**
- Git documentation, **git-reflog**
- Git documentation, **git-worktree**
- Git documentation, **git-rev-parse**
- Git documentation, **git-count-objects**
- Git LFS official manual, **git-lfs-prune(1)**
- Git LFS official manual, **git-lfs-config(5)**
- Git LFS official manual, **git-lfs(1)**
