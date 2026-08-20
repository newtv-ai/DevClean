# PyTorch Hub storage audit

Audited/implemented: 2026-08-20

## Product conclusion

PyTorch Hub storage is useful disk context, but the current public contract does **not** expose a complete cache inventory plus exact cleanup API comparable to Hugging Face Hub. DevClean therefore keeps the current PyTorch Hub pass **REPORT_ONLY / protected**.

The product adds a read-only **PyTorch Hub 存储概览…** surface that can inspect one user-selected Hub root, explain known top-level classes, and report logical size without granting deletion authority.

Current lanes:

- `trusted_list`: **KEEP_PROTECTED**;
- `checkpoints/`: **REPORT_ONLY**;
- repo-like top-level directories: **REPORT_ONLY**;
- top-level `.zip` files: **REPORT_ONLY**;
- any other top-level object: **REPORT_ONLY**;
- whole Hub root: protected;
- no age/size/name rule creates deletion authority.

## Primary source

This pass is pinned to current PyTorch source commit:

`f23687d10f18836f8b530ce5aa9687b918660920`

Primary files:

- `torch/hub.py`
- `test/test_hub.py`

Relevant source URLs:

- https://github.com/pytorch/pytorch/blob/f23687d10f18836f8b530ce5aa9687b918660920/torch/hub.py
- https://github.com/pytorch/pytorch/blob/f23687d10f18836f8b530ce5aa9687b918660920/test/test_hub.py

## What PyTorch currently guarantees

`torch.hub` publicly exports download/load/list/help plus `get_dir()` / `set_dir()`. It does not publicly export a cache inventory, prune, remove, garbage-collection, or object-level delete interface.

Default Hub location is:

1. `$TORCH_HOME/hub` when `TORCH_HOME` is set;
2. otherwise `$XDG_CACHE_HOME/torch/hub` when `XDG_CACHE_HOME` is set;
3. otherwise `~/.cache/torch/hub`.

But `torch.hub.set_dir()` can replace this path as process-local runtime state. An external DevClean process cannot prove that another Python process did or did not call `set_dir()`. The UI therefore calls the environment/default path only a **candidate** and lets the user explicitly select a different root.

DevClean does not import PyTorch or execute Python merely to discover storage.

## Why repo directories are not exact object identities

For GitHub-backed Hub loads, current source constructs a cached repo directory as:

`owner_name_branch = "_".join([repo_owner, repo_name, normalized_ref])`

where `/` inside the ref is first replaced with `_`.

That encoding is not reliably reversible:

- owner names can contain underscores;
- repository names can contain underscores;
- refs can contain underscores;
- refs can contain `/`, which is converted to `_`;
- there is no adjacent durable manifest storing the original owner/repo/ref tuple.

Therefore a directory such as `owner_repo_feature_branch` cannot safely be turned back into one exact GitHub repository/ref identity by DevClean.

The trust path makes filename guessing even weaker. Current `_check_repo_is_trusted()` treats **all existing top-level directories** in the Hub root as legacy trusted repo names. A directory's mere presence is therefore not a proof that DevClean can reconstruct its source identity or lifecycle.

Even when a directory contains `hubconf.py`, that only indicates runnable Hub-style content. Executing `hubconf.py` to discover meaning would run project code and is outside DevClean's cleanup authority.

## Why `force_reload` is not a cleanup API

`force_reload=True` causes PyTorch Hub, for a user-supplied repository identity, to discard the current repo cache and download/extract a replacement. Internally the source uses private `_remove_if_exists()` around the transient zip/extracted directory and cached repo directory.

This is not a storage-reclamation interface:

- it requires the caller already to know the source repo/ref;
- it immediately downloads a replacement;
- `_remove_if_exists()` is private implementation detail, not a public cleanup contract;
- there is no vendor inventory mapping arbitrary existing directories back to exact repo/ref identities.

DevClean therefore does not wrap `force_reload` or private `_remove_if_exists()` as a cleanup command.

## Why `checkpoints/` remains REPORT_ONLY

`load_state_dict_from_url()` defaults to storing downloaded weights under:

`<hub_dir>/checkpoints/<filename>`

The filename normally comes from the URL basename, but the caller can supply an arbitrary `file_name` override.

The current implementation persists the file itself, not a durable provenance record containing:

- original URL;
- expected hash unless the caller separately provided/checks one at download time;
- model/repository identity;
- whether the file was manually copied into the directory;
- whether the source is still reachable.

A large `.pth`, `.pt`, `.bin`, or other file under `checkpoints/` can therefore be a deliberate offline working set or manually managed artifact. DevClean cannot prove it is disposable merely from its location, suffix, age, or size.

The UI reports checkpoint logical size but provides no delete button.

## Why top-level zip files remain REPORT_ONLY

During a repo download, current source temporarily uses:

`<hub_dir>/<normalized_ref>.zip`

and normally removes it after extraction. A crash or failed run can leave a zip behind.

However there is no persistent provenance marker proving that an arbitrary top-level `.zip` was created by that code path. A custom Hub root is user-controlled storage. DevClean therefore does not automatically classify an existing zip as disposable transfer residue.

A future PyTorch-owned temp manifest or exact cleanup API could change this conclusion.

## Trust state is protected

`trusted_list` records repositories the user has trusted. PyTorch also considers existing legacy repo directories during trust checks.

DevClean therefore treats `trusted_list` as persistent security/trust state, not cache. The read-only inventory never modifies it.

## Read-only scanning boundary

The new inventory:

- inspects only one explicit absolute Hub root;
- does not import `torch`;
- does not execute `hubconf.py` or any project Python;
- does not make network requests;
- does not follow symlink/junction/reparse/cloud-placeholder boundaries;
- caps traversal at 200,000 filesystem objects;
- reports logical bytes only;
- never represents logical size as guaranteed physical reclaim;
- exposes no mutation function or delete button.

Known top-level names are explanatory labels only. They never become mutation authority.

## Why this is still useful product functionality

PyTorch model caches can be large. A read-only source-aware view still answers important questions safely:

- which Hub root DevClean is inspecting;
- whether the default path even exists;
- how much space is under `checkpoints/`;
- how much space is in repo-like/unknown directories;
- whether `trusted_list` exists;
- whether reparse/cloud boundaries prevented a complete scan;
- why DevClean intentionally refuses to delete these objects automatically.

This is preferable to a generic `%USERPROFILE%\.cache\torch` delete rule that would overstate object identity and rebuildability.

## Revisit triggers

A destructive PyTorch Hub lane should be reconsidered only if PyTorch provides one or more of:

1. a public machine-readable cache inventory with exact stable object IDs;
2. a public exact repo/checkpoint removal or prune API/CLI;
3. durable checkpoint provenance that binds local files to source URL/hash and rebuild semantics;
4. a dedicated vendor-owned temporary-download lifecycle with exact object identification;
5. another equally strong non-executing mechanism that proves mutation scope and postconditions.

Until then, PyTorch Hub remains read-only REPORT_ONLY/protected.

## Validation

Normal DevClean final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
