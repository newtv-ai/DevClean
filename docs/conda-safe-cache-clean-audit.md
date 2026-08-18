# Conda safe cache-clean audit

Audited: 2026-08-18

## Product conclusion

Conda's `pkgs` directory must not be treated as one disposable folder. It mixes downloaded archives and index data with extracted package trees that may be link sources for installed environments.

DevClean therefore exposes only the narrow vendor operation that is broadly safe for a disk cleaner:

- `conda clean --tarballs --index-cache --yes --json`

It deliberately does **not** request `--packages`, `--all`, or `--force-pkgs-dirs`.

This is deterministic local knowledge and does not need AI. A package-cache root at or above 1 GiB is selected by default as a benefit heuristic; smaller roots remain understood and selectable. The threshold only decides whether the operation is likely worth running.

## Why the boundary is narrow

Current Conda documentation describes `conda clean` as removing unused packages and caches. It defines:

- `--index-cache` as removing the index cache;
- `--tarballs` as removing cached package tarballs;
- `--packages` as removing unused packages from writable package caches, but explicitly warns that this does not check packages installed using symlinks back to the package cache;
- `--force-pkgs-dirs` as removing all writable package caches and explicitly warns that it can break symlink-backed environments;
- `--all` as including unused cache packages in addition to index cache, tarballs, lock files, and logs.

The warning makes `--packages` unsuitable for DevClean's universal deterministic lane. `--force-pkgs-dirs` is even broader. Since `--all` includes package removal, it is also excluded.

## Execution contract

Before mutation DevClean:

1. re-resolves Conda's current audited package-cache roots;
2. requires the selected path to match one of those roots exactly;
3. refuses while Conda/Mamba activity is detected;
4. scopes Conda to the selected root with `CONDA_PKGS_DIRS`;
5. invokes the same configured Conda executable with `conda info --json`;
6. requires the returned `pkgs_dirs` to contain that exact root;
7. only then runs `conda clean --tarballs --index-cache --yes --json`;
8. reports vendor failures and never falls back to raw deletion.

The generic scanner continues to protect the package-cache tree from recursive deletion.

## Custom locations

Conda officially supports custom `pkgs_dirs` and `CONDA_PKGS_DIRS`. DevClean therefore validates effective cache roots instead of assuming one hard-coded Miniconda/Anaconda installation path.

## Sources

- Conda documentation, **conda clean**: removal targets, safe cache switches, and explicit symlink warnings for package-cache removal.
- Conda documentation, **Settings — pkgs_dirs**: package-cache locations and `CONDA_PKGS_DIRS` override behavior.
- Conda documentation, **conda info**: `--json` is a supported programmatic output mode used for execution-time root confirmation.
