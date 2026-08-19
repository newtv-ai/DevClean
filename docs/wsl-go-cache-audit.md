# WSL Go cache maintenance audit

## Decision

Do not expose one monolithic `go clean` action. Go's cache-related flags have
materially different semantics and must remain separate lanes.

Initial WSL implementation candidates:

- **build cache (`GOCACHE`)** — `DETERMINISTIC_CANDIDATE`, but low-benefit by
  default because Go already ages old entries itself and its documentation says
  explicit cleaning is normally unnecessary;
- **module download cache (`GOMODCACHE`)** — `USER_REVIEW`, because it contains
  downloaded modules including unpacked versioned dependency source and can be
  valuable for old branches, offline work, or slow networks;
- **test result cache (`-testcache`)** — `REPORT_ONLY / no storage action` in the
  first implementation because the operation expires test results inside the
  build cache rather than defining a separate high-value storage root;
- **fuzz cache (`-fuzzcache`)** — `USER_REVIEW` semantics, but implementation is
  deferred because Go explicitly warns that removal can temporarily reduce fuzz
  effectiveness and the cache is not separately exposed as an independently
  measurable root.

No AI is needed for any of these semantics.

## Primary Go contracts

Current Go command documentation states:

- `go env [-json] ...` reports effective environment values, and `-json`
  provides structured output;
- `GOCACHE` is the absolute directory used for build-cache information;
- `GOMODCACHE` is the directory used for downloaded modules;
- `GOCACHEPROG` can point Go at an external build-cache program;
- the build cache is safe for concurrent Go invocations;
- Go periodically deletes build-cache entries that have not been used recently;
- `go clean -cache` removes the entire build cache, but explicit cleaning is not
  normally necessary;
- `go clean -modcache` removes the entire module download cache, including
  unpacked source code of versioned dependencies;
- `go clean -testcache` expires cached test results without removing ordinary
  build results;
- `go clean -fuzzcache` removes fuzzing values that expanded coverage and may
  make fuzzing temporarily less effective.

Primary sources:

- https://pkg.go.dev/cmd/go
- https://go.dev/src/cmd/go/internal/clean/clean.go
- https://go.dev/src/cmd/go/internal/cache/default.go

## Build-cache lane

The build cache is reproducible acceleration state, so the semantic ownership is
deterministic. However, Go already performs periodic aging and explicitly says
manual cleaning should not usually be necessary.

Therefore DevClean should:

1. ask the exact Go executable for `go env -json GOCACHE GOCACHEPROG`;
2. refuse executable authority when `GOCACHEPROG` is non-empty, because an
   external cache program can have storage/lifecycle semantics outside the
   selected WSL distribution;
3. require `GOCACHE` to be one absolute non-root POSIX path;
4. require the WSL root-filesystem mutation-scope proof immediately before
   mutation;
5. re-confirm Go version, `GOCACHE`, and `GOCACHEPROG` immediately before
   mutation;
6. use only `go clean -cache`, with `GOCACHE` pinned in the command environment
   or another source-backed exact scoping mechanism;
7. keep a large-size threshold only as a *benefit* recommendation, never as
   deletion authority.

The existing native Go lane uses 1 GiB as that recommendation threshold; the WSL
lane can retain the same product heuristic once logical size can be measured
without granting raw deletion authority.

## Module-cache lane

`GOMODCACHE` is vendor-managed but not merely disposable acceleration state from
the user's perspective: it contains downloaded and unpacked dependency source.
Go can recreate it from module sources, but network availability, private-module
credentials, old branches, and offline work can make retaining it valuable.

Therefore it remains `USER_REVIEW`:

1. ask Go for the exact effective `GOMODCACHE`;
2. require an absolute non-root POSIX path;
3. require the WSL root-filesystem mutation-scope proof;
4. re-confirm Go version and path immediately before mutation;
5. use only `go clean -modcache` with the exact cache location pinned;
6. never preselect it from age or size alone.

## External build cache

A non-empty `GOCACHEPROG` is a hard execution boundary for the build-cache lane.
The Go docs define it as a command implementing an external build cache. DevClean
cannot infer whether that program stores data locally, remotely, or in another
lifecycle domain, so the build cache becomes `REPORT_ONLY` until a future
adapter audits that exact external program.

## Process and concurrency policy

Although Go documents its normal build cache as safe for concurrent Go
invocations, the initial DevClean WSL implementation should retain the existing
native product guard around destructive cache-clean operations: if Go/gopls
activity cannot be safely ruled out, do not clean. This is conservative and
keeps behavior aligned across native Windows and WSL lanes.

The guard must not kill processes or escalate privileges.

## Deliberate exclusions

DevClean does **not**:

- run bare `go clean` against a project tree;
- expose `-i`, `-r`, package arguments, or source-directory cleanup through this
  storage lane;
- combine `-cache`, `-modcache`, `-testcache`, and `-fuzzcache` into one action;
- treat `GOPATH`, `GOROOT`, installed binaries, module source, or project output
  as generic cache;
- mutate an external `GOCACHEPROG` backend;
- raw-delete `GOCACHE` or `GOMODCACHE` paths;
- use shell command strings or Windows-side path deletion as fallback;
- promise that WSL logical bytes released equal Windows VHD bytes reclaimed.

## Implementation order

1. land the shared WSL root-filesystem mutation-scope hardening;
2. implement build-cache inventory and conservative `go clean -cache` lane;
3. implement module-cache `USER_REVIEW` separately;
4. leave test/fuzz cache actions deferred until their storage value and user
   tradeoffs justify separate product surfaces.
