# WSL Go module-cache maintenance

## Lane

The WSL Go module download cache (`GOMODCACHE`) is **USER_REVIEW**.

Go can recreate the cache by downloading modules again, but the cache contains
both downloaded module artifacts and unpacked source code for versioned
dependencies. Keeping it can be valuable for old branches, offline work, slow
networks, or private modules whose future access may depend on credentials or
network reachability.

This is technically understood storage, so AI is not needed. The user decides
whether the reclaim benefit is worth losing the local dependency cache.

## Vendor contract

DevClean asks the selected distribution's exact `go` command for:

```text
go version
go env -json GOMODCACHE
```

Go's module reference defines the module cache as the directory where the `go`
command stores downloaded modules. The default is `$GOPATH/pkg/mod`, but
`GOMODCACHE` may redirect it elsewhere. The cache has no maximum size and Go
does not automatically remove its contents.

For cleanup, Go documents `go clean -modcache`. The command removes the entire
module cache, including unpacked source code of versioned dependencies. Go
specifically recommends its own clean command rather than raw recursive deletion
because module-cache files may be read-only.

Primary sources:

- https://go.dev/ref/mod
- https://pkg.go.dev/cmd/go
- https://go.dev/src/cmd/go/main.go
- https://go.dev/src/cmd/go/internal/base/goflags.go
- https://go.dev/src/cmd/go/internal/cfg/cfg.go
- https://go.dev/src/cmd/go/internal/clean/clean.go

## GOFLAGS widening guard

Go applies matching `GOFLAGS` defaults to the clean command before parsing its
explicit command line. Therefore an inherited or `go env -w` setting can contain
other clean booleans.

Setting `GOFLAGS=` in the process environment is not a reliable neutralizer for
a persistent `go env -w GOFLAGS=...`: current Go `cfg.Getenv` falls back to the
user's `go/env` file when the OS environment value is empty.

DevClean instead makes the final command line authoritative by explicitly
setting every destructive clean boolean:

```text
env GOMODCACHE=<exact-path> go clean \
  -i=false -r=false \
  -cache=false -testcache=false -modcache=true -fuzzcache=false
```

Go parses these command-line values after applying `GOFLAGS`, so the matching
flags are overridden without editing or disabling the user's Go configuration.
No package arguments are supplied.

## Mutation contract

Immediately before mutation DevClean:

1. re-reads Go version and `GOMODCACHE`;
2. requires them to match the state the user reviewed;
3. refuses while `go` or `gopls` activity is visible and fails closed when
   process state cannot be checked;
4. requires the exact `GOMODCACHE` to pass the WSL root-filesystem device
   identity proof;
5. pins the exact module cache and invokes only the fully constrained clean
   command above.

The shared WSL environment wrapper allows only audited cache variables and
rejects generic overrides such as `PATH`, `LD_PRELOAD`, `GOENV`, and `GOFLAGS`.
Direct generic `env` execution is also blocked at the public execution boundary.

After the vendor command succeeds, DevClean asks Go for the identity again. A
changed Go version or changed `GOMODCACHE` causes the result to be treated as
unconfirmed.

## Local-storage boundary

Vendor ownership does not automatically grant physical mutation authority.

A `GOMODCACHE` redirected to `/mnt/c`, removable/network storage, or another
separately mounted filesystem remains reportable but is not executable. The
mutation path must resolve onto the selected distribution's root filesystem.

## Deliberate exclusions

This lane does not:

- clean the Go build cache (`GOCACHE`);
- leave `-cache`, `-testcache`, `-fuzzcache`, `-i`, or `-r` under inherited
  default control;
- run bare `go clean` or pass package arguments;
- delete `GOPATH` as a whole;
- delete project source, `go.mod`, `go.sum`, `go.work`, or installed binaries;
- raw-delete module-cache files;
- mutate a module cache on another mounted filesystem;
- edit or disable the user's persistent Go environment configuration;
- use AI to decide known Go module-cache semantics;
- promise that released Linux logical bytes will immediately shrink the Windows
  WSL VHD file.

## Product behavior

The UI labels this lane **USER_REVIEW**, never preselects or auto-runs it, and
shows the exact Go version, distribution, and `GOMODCACHE` path before asking for
explicit confirmation.
