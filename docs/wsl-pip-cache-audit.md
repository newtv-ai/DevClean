# WSL pip cache authority audit

## Decision

The cache selected by `pip cache dir` inside one exact WSL distribution is a
**DETERMINISTIC_CANDIDATE** when the same pip entry point also supports the
vendor-owned `pip cache info` and `pip cache purge` commands.

This lane does not use AI and does not grant raw Linux or Windows filesystem
deletion authority.

## Source-backed semantics

Current pip documentation states that pip maintains HTTP response and locally
built wheel caches, that the cache layout is an implementation detail, and that
`pip cache dir` returns the cache directory pip is configured to use. It also
documents `pip cache info` for cache overview and `pip cache purge` for removing
all items from the cache.

Primary sources:

- https://pip.pypa.io/en/stable/topics/caching/
- https://pip.pypa.io/en/stable/cli/pip_cache/
- https://pip.pypa.io/en/stable/cli/pip/

Microsoft documents running a specific WSL distribution explicitly with
`wsl --distribution <Distribution Name>` and running Linux commands from
Windows through `wsl.exe`.

- https://learn.microsoft.com/windows/wsl/basic-commands
- https://learn.microsoft.com/windows/dev-environment/wsl-interop

## Execution contract

DevClean must:

1. require an exact distribution returned by the existing WSL inventory;
2. discover a pip entry point from a code-defined sequence, preferring
   `python3 -m pip` / `python -m pip` over wrapper executables;
3. ask that exact entry point for `cache dir`, `--version`, and `cache info`;
4. require `cache dir` to return one absolute non-root POSIX path;
5. retain the exact distribution, entry point, pip version, and effective cache
   path as the mutation identity;
6. repeat the inventory immediately before mutation and refuse if that identity
   changed;
7. fail closed unless the selected distribution can report a process snapshot
   and no pip/Python-pip process is visible;
8. execute only the same pip entry point with `cache purge`;
9. re-inventory afterward and refuse to claim a confirmed result if the pip or
   cache identity changed.

The process boundary is the already-audited argv-only WSL adapter. No shell
command string is constructed.

## Deliberate non-features

DevClean does **not**:

- parse or delete `~/.cache/pip` by convention;
- assume that `~/.cache/pip` is the effective path when XDG or pip config moves it;
- inspect pip's internal HTTP/wheel directory layout;
- run `rm`, `find`, `xargs`, PowerShell, `sh -c`, or `bash -c` as a fallback;
- change `PIP_CACHE_DIR`, pip config, the WSL default distribution, or distro user;
- use AI to decide whether pip's own cache is cache;
- claim that Linux logical bytes freed equal Windows host bytes reclaimed.

## Product behavior

The user explicitly selects one registered distribution before DevClean probes
pip. If that distribution was stopped, the UI warns that checking pip requires
executing a command in that distribution and may start it.

The UI displays pip's raw `cache info` output instead of parsing its human text
into a security boundary. The initial WSL lane therefore does not auto-select or
auto-run a purge based on a parsed size threshold.

A successful purge can make later installs re-download HTTP responses or rebuild
wheels. For WSL 2, freeing files inside the Linux filesystem does not by itself
justify a promise that the Windows-side virtual disk file shrinks by the same
amount. The separate WSL sparse/VHD audit remains REPORT_ONLY.
