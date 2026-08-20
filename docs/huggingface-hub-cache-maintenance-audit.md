# Hugging Face Hub cache maintenance audit

Audited/implemented: 2026-08-20

## Product conclusion

Hugging Face Hub cache is **download/cache state**, but cached models, datasets and exact revisions can still be intentional offline or reproducibility working sets. Redownloadability therefore does not make the whole cache automatically disposable.

DevClean's narrow product lanes are:

- one exact cached repo: **USER_REVIEW**;
- one exact cached revision: **USER_REVIEW**, only when the full 40-hex commit hash is proven globally unique in the complete current inventory;
- `hf cache prune` detached revisions + incomplete downloads: **USER_REVIEW**;
- Xet cache: **REPORT_ONLY / vendor-managed** for this pass;
- assets cache: **REPORT_ONLY / vendor-managed** for this pass;
- `HF_HOME` as a whole, authentication token state and raw cache-directory deletion: protected.

No age/size threshold creates deletion authority.

## Current vendor contract

Current `huggingface_hub` CLI/source establishes:

- `hf cache ls --format json` inventories cached repositories;
- `hf cache ls --revisions --format json` inventories exact cached revisions including full commit hash, refs, snapshot path and vendor cache size;
- `hf cache rm <repo-cache-id>` removes an exact cached repository;
- `hf cache rm <40-hex-revision>` removes an exact revision;
- `hf cache rm --dry-run` previews exact deletion scope;
- `hf cache prune` removes revisions with no refs plus incomplete downloads;
- `hf cache prune --dry-run` previews those counts and vendor-estimated freed size;
- cache deletion is performed through Hugging Face's own cache manager rather than by externally removing `blobs/`, `refs/` or `snapshots/` directories.

Primary sources, audited against `huggingface/huggingface_hub` commit `7576ccead92135b25c09fc5353784bb1f53db0df`:

- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/docs/source/en/package_reference/cli.md
- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/docs/source/en/guides/manage-cache.md
- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/docs/source/en/guides/manage-cache.md
- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/src/huggingface_hub/cli/cache.py
- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/src/huggingface_hub/constants.py
- https://github.com/huggingface/huggingface_hub/blob/7576ccead92135b25c09fc5353784bb1f53db0df/src/huggingface_hub/utils/_cache_manager.py

## Why exact repo removal is USER_REVIEW

`hf cache rm model/foo`, `dataset/foo`, or `space/foo` is a strong object boundary: Hugging Face itself resolves the repo's cached revisions and owns the deletion strategy.

But an entire repo can represent:

- a deliberately downloaded offline model;
- a local dataset snapshot needed without network access;
- a cached revision set used to reproduce an experiment;
- a large artifact the user would rather not download again.

DevClean therefore does not auto-select repos based on last access, size, ref state or redownloadability. The UI shows vendor identity/size and requires explicit confirmation.

Before actual removal DevClean:

1. re-inventories the complete exact Hub cache;
2. requires the same exact `hf` executable identity and exact cache-root identity;
3. requires the complete repo/revision state to still equal the user's reviewed snapshot;
4. refuses while Hugging Face/Transformers/Diffusers-related activity is visible;
5. runs `hf cache rm <exact-cache-id> --cache-dir <exact-root> --dry-run --yes --format json`;
6. requires dry-run scope to equal exactly one repo and exactly the reviewed revision count;
7. repeats fresh validation before the real command;
8. runs the same exact vendor target without `--dry-run`;
9. requires vendor result counts to equal dry-run counts;
10. re-inventories and requires the exact repo ID to be absent.

No filesystem fallback exists.

## Why exact revision deletion needs a uniqueness gate

Current Hugging Face CLI source builds its revision lookup as a dictionary keyed only by full commit hash. If the same 40-hex commit hash appears in more than one cached repo, later entries overwrite earlier entries in that lookup.

DevClean therefore does **not** assume a full commit hash is globally unique merely because it is cryptographically strong. Revision deletion is executable only when:

- the vendor inventory returned a full 40-hex commit;
- that hash occurs exactly once across the complete current revision inventory;
- the `hf cache ls` pass produced no warnings that would make completeness uncertain;
- the repo type/cache ID is a recognized current model/dataset/space form;
- the vendor dry-run resolves exactly one revision and the expected whole-repo count (`1` only when this is the repo's last revision, otherwise `0`).

If any of those fail, the revision remains visible but protected. Repo-level USER_REVIEW can still be used when its own exact vendor target is valid.

## Why vendor prune remains USER_REVIEW

Current `hf cache prune` removes:

- revisions with zero refs (detached revisions);
- incomplete downloads.

Incomplete downloads are naturally disposable transfer state. Detached revisions are more subtle: a user can intentionally cache an exact commit hash that is not currently named by a branch/tag ref. Such a revision may be the exact artifact needed for offline/reproducible work.

Because current vendor prune combines these classes in one operation, DevClean does not label the broad prune deterministic. The UI first performs vendor `--dry-run`, then warns explicitly that detached revisions can be intentional exact-commit working sets.

Before execution DevClean requires the fresh detached revision set and vendor dry-run counts/size to remain identical. The actual vendor result counts must also match the reviewed dry-run, and reviewed detached commits must be absent afterward.

## Cache root and CLI binding

Hugging Face's current constants define the Hub cache through the normal `HF_HOME` / `XDG_CACHE_HOME` defaults with `HUGGINGFACE_HUB_CACHE` compatibility and modern `HF_HUB_CACHE` override. DevClean keeps the existing source-backed environment precedence and requires one exact resolved Hub root.

The root must be:

- an existing ordinary directory;
- not a symlink, junction, reparse point or cloud placeholder;
- on local fixed storage;
- bound to a stable volume/file identity.

The `hf` command is also resolved to one exact ordinary local executable and bound to stable file identity. Destructive commands use that exact path, never a newly resolved ambient `PATH` command.

`--cache-dir <exact-root>` is passed to every vendor inventory/dry-run/mutation. `HF_HUB_CACHE` is additionally fixed in the subprocess environment to the same root.

## Accounting and shared blobs

The Hub cache stores repository data through `blobs`, `refs` and `snapshots`. Snapshot files normally reference shared blobs, so summing apparent snapshot file sizes can double-count shared data. On Windows, when symlinks are unavailable, Hugging Face may instead keep duplicated files in snapshots.

For exact repo/revision decisions DevClean therefore displays **Hugging Face's own cache accounting strings** and vendor dry-run `size/freed` evidence. It does not compute deletion benefit by following snapshot links.

The legacy coarse cache-root display is also tightened so directory walking never follows symlink targets. Even so, coarse logical bytes and vendor `freed` values are explanatory evidence only; DevClean does not promise an equal immediate increase in Windows physical free space.

Xet cache is a separate transfer/chunk cache and can have lifecycle independent of the Hub repo snapshots. Removing a Hub repo is not represented as guaranteed removal of equivalent Xet bytes.

## Xet/assets and HF_HOME

This audit grants no raw deletion authority to:

- the whole `HF_HOME` directory;
- authentication token files;
- Xet cache roots;
- assets cache roots used by downstream libraries;
- raw `models--*`, `datasets--*`, `spaces--*`, `blobs`, `refs`, `snapshots`, or `.locks` paths.

Xet/assets are shown read-only so users can understand where space is located, but a future destructive lane requires a dedicated vendor lifecycle/API or a separately audited exact object boundary.

## Deliberate exclusions

No authority is granted to:

- delete a repo/revision because it is old, large, detached or redownloadable;
- treat detached revision as synonymous with unused;
- run broad raw `%USERPROFILE%\.cache` cleanup;
- manually unlink shared Hub blobs/snapshots;
- delete Hub data while a relevant model/download process is active;
- accept short hashes or tag guesses as revision deletion targets;
- delete a revision when the full hash appears more than once or inventory warnings make uniqueness proof incomplete;
- edit tokens/auth configuration;
- make Xet/assets destructive in this pass;
- use AI to invent a cache target or command;
- claim logical/vendor cache accounting as guaranteed physical disk reclaim.

## Validation

Normal DevClean validation remains mandatory on the final head: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
