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

For cleanup, Go documents:

```text
go clean -modcache
```

The command removes the entire module cache, including unpacked source code of
versioned dependencies. Go specifically recommends its own clean command rather
than raw recursive deletion because module-cache files may be read-only.

Primary sources:

- https://go.dev/ref/mod
- https://pkg.go.dev/cmd/go
- https://go.dev/src/cmd/go/internal/clean/clean.go

## Mutation contract

Immediately before mutation DevClean:

1. re-reads Go version and `GOMODCACHE`;
2. requires them to match the state the user reviewed;
3. refuses while `go` or `gopls` activity is visible and fails closed when
   process state cannot be checked;
4. requires the exact `GOMODCACHE` to pass the WSL root-filesystem device
   identity proof;
5. pins the exact module cache and clears inherited Go command flags:

```text
env GOMODCACHE=<exact-path> GOFLAGS= go clean -modcache
```

`GOFLAGS` is forced empty so persistent default flags cannot widen this action to
other Go clean lanes. The shared WSL environment wrapper allows only audited
cache variables and rejects generic overrides such as `PATH`, `LD_PRELOAD`, and
`GOENV`.

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
- invoke `-cache`, `-testcache`, or `-fuzzcache`;
- run bare `go clean` or pass package arguments;
- delete `GOPATH` as a whole;
- delete project source, `go.mod`, `go.sum`, `go.work`, or installed binaries;
- raw-delete module-cache files;
- mutate a module cache on another mounted filesystem;
- use AI to decide known Go module-cache semantics;
- promise that released Linux logical bytes will immediately shrink the Windows
  WSL VHD file.

## Product behavior

The UI labels this lane **USER_REVIEW**, never preselects or auto-runs it, and
shows the exact Go version, distribution, and `GOMODCACHE` path before asking for
explicit confirmation.
