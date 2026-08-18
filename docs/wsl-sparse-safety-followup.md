# WSL sparse-VHD safety follow-up

Audited: 2026-08-19

## Decision

DevClean must **not** currently offer an executable `wsl --manage <Distro> --set-sparse true` action, and must never append `--allow-unsafe` on the user's behalf.

This tightens Lane B from `docs/wsl-storage-audit.md`. Sparse-VHD state remains a WSL-owned storage policy, but current upstream safety evidence is not strong enough to turn that fact into mutation authority.

## Why the lane is blocked

Current Microsoft WSL configuration documentation still describes `sparseVhd` under the `[experimental]` section. The documented setting controls whether newly created VHDs are created sparse automatically; it is not a general guarantee that converting an existing distribution is risk-free.

The current open-source WSL client continues to define and parse an `--allow-unsafe` option alongside `--manage ... --set-sparse`. Microsoft WSL issue #13075 also records WSL 2.5.8 refusing sparse conversion because of a potential data-corruption risk unless the user explicitly supplied that unsafe override. A WSL collaborator confirmed the corresponding forced command syntax in that issue.

For DevClean, an upstream option literally named `--allow-unsafe` is a hard stop. The product must not hide, automate, preselect, or normalize away a vendor safety override.

## Product rule

Until a future source audit establishes that the installed stable WSL version exposes sparse conversion without an unsafe override and Microsoft documents the operation as supported for the target case:

- inventory WSL version and distro state only;
- sparse capability may be reported as informational state if it can be queried through a stable vendor surface;
- do not execute `--set-sparse true` or `--set-sparse false`;
- never execute `--allow-unsafe`;
- do not edit `.wslconfig` automatically to enable experimental `sparseVhd`;
- do not infer current VHD sparsity from file attributes or guessed `ext4.vhdx` paths;
- do not use raw `Optimize-VHD`, DiskPart, registry paths, package directories, or direct VHD mutation as a workaround.

This is **REPORT_ONLY**, not USER_REVIEW, because user confirmation cannot make an upstream data-corruption warning safe enough for DevClean to automate.

## Re-entry criteria

The sparse lane can be reconsidered only when all of the following can be shown for the supported WSL range:

1. the operation is present in an installed stable WSL capability/help surface;
2. Microsoft documents the exact per-distribution command and supported preconditions;
3. normal execution does not require `--allow-unsafe` or an equivalent bypass;
4. the target distribution can be identified exactly through WSL itself;
5. running/offline preconditions can be checked without guessing backing-file paths;
6. regression tests can prove DevClean never widens the vendor command or touches another distro;
7. post-operation state can be verified through a supported vendor surface.

Even then, changing storage policy should remain an explicit user action rather than an automatic cleanup recommendation.

## Physical compaction remains separate

This follow-up does not grant raw VHD compaction authority. Host physical-space reclamation remains a separate Lane D problem. DevClean should prefer a stable WSL-owned compaction command if Microsoft ships and documents one for supported releases; until that contract is established, the existing rule remains unchanged: no guessed VHD path, no raw host-side compaction button, and no unregister/export-import shortcut for routine cleanup.

## Primary sources checked

- Microsoft Learn / MicrosoftDocs WSL configuration documentation (`[experimental] sparseVhd`)
- microsoft/WSL current command-line source (`--manage`, `--set-sparse`, `--allow-unsafe`)
- microsoft/WSL issue #13075, including WSL's potential-data-corruption refusal and maintainer guidance
