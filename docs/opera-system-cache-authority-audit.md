# Opera System Cache authority audit

Audited: 2026-08-20

## Product conclusion

DevClean's generic age/size-driven raw deletion authority for Opera's `System Cache` directory is removed.

Current lane:

- Opera `System Cache`: **REPORT_ONLY / browser-managed**;
- the path remains semantically identifiable for explanation;
- age, size and process-idle state do not make it a generic delete candidate;
- the directory is no longer a whole-tree DevClean cleanup root;
- existing separately audited Chromium-derived Opera HTTP/code/GPU/shader/script-cache rules are unchanged;
- an explicitly dedicated `DEVCLEAN_OPERA_DISK_CACHE_DIR` remains a separate exact cache-root contract and is not changed by this audit.

The correction is narrow. `System Cache` looks cache-like and Opera support material treats it as regenerable troubleshooting storage, but that does not establish the former DevClean policy of `14 days + 8 MiB -> raw whole-tree deletion`.

## Current vendor evidence

Opera's current public Help documentation describes clearing **cached images and files** through Opera's own browsing-data UI. It explains that the cache stores temporary website/application data and that clearing it can release disk space.

Primary current public documentation:

- `https://help.opera.com/?p=private.html`

The documentation does not define an on-disk `System Cache` object contract, a fourteen-day expiration rule, or a supported filesystem-level whole-tree deletion API for that directory.

Opera community troubleshooting posts do describe closing Opera and manually deleting `Cache` / `System Cache`, and some advice is intentionally broader, such as deleting folders with `cache` in their name. That is useful evidence that the data can be regenerated in troubleshooting scenarios. It is not a stable machine-readable lifecycle suitable for an unattended cleaner:

- it is manual support guidance rather than a bounded maintenance API;
- it assumes a human has identified the correct profile/cache path;
- it does not define the former fourteen-day threshold;
- broader community advice can intentionally trade retained browser acceleration state for troubleshooting reset;
- it does not supply a fresh object manifest or concurrency/postcondition contract.

DevClean therefore uses the vendor evidence for **classification**, not for inventing destructive authority.

## Why the former rule was too broad

The former `_OPERA_SYSTEM_CACHE_RULE` granted `DecisionOwner.TOOL` when a matching `System Cache` subtree was at least 14 days old and at least 8 MiB. It also set `allow_whole_tree=True`.

That policy was independently invented by DevClean. The current public vendor material does not establish either threshold, and it does not state that every environment-derived Opera edition/profile `System Cache` path is an interchangeable whole-tree cleanup object.

The rule also bypassed Opera's own browsing-data lifecycle. Even where cache files are regenerable, vendor-managed cache eviction and a user-initiated browser cache clear are not equivalent to a generic filesystem tree deletion chosen from mtime and logical size.

Under DevClean's current execution standard, **regenerable is necessary evidence but not sufficient authority**.

## Scope separation retained

This audit does not collapse all Opera cache semantics into REPORT_ONLY.

DevClean already has Chromium-derived rules for specific browser-generated cache classes. Those rules remain separate and are not reclassified merely because the Opera-specific `System Cache` rule was under-specified.

Likewise, an explicit dedicated disk-cache root supplied through DevClean's dedicated override remains a distinct boundary. The change is only that the additional Opera-specific `System Cache` folder no longer gains raw whole-tree authority from its name plus age/size.

Profile history, sessions, login state, local storage and recovery copies remain protected under their existing rules.

## No learned-rule bypass

Application semantics outrank generic learned filename/path verdicts.

After this correction, AI or user learned verdicts cannot manufacture a generic delete rule under Opera `System Cache`. Otherwise a path protected because its lifecycle is insufficiently bounded could immediately regain the same raw authority through a lower-confidence heuristic layer.

Regression tests keep the existing positive Opera HTTP-cache learned behavior separate from this protected Opera-specific directory.

## Revisit trigger

A future positive Opera `System Cache` lane should not restore the former fixed age/size rule. Reconsider only if Opera exposes a sufficiently exact source/vendor contract, for example:

1. an official API/CLI or exact browser maintenance operation that targets the reviewed cache class without also clearing cookies, history, passwords, site state or unrelated profiles;
2. a source-backed effective path/object model that binds the exact Opera edition/profile and cache root without guessing from directory names;
3. a complete mutation scope that remains inside that reviewed cache class;
4. browser/process/concurrency protection and fresh revalidation immediately before mutation;
5. a postcondition proving the reviewed cache operation completed without widening into persistent profile state;
6. logical reclaim reporting that does not promise identical physical free-space recovery.

Until then, `System Cache` is visible and explainable but non-executable.

## Validation

This correction removes Opera `System Cache` from audited generic tool roots, keeps its classification as protected/browser-managed state, and adds regression coverage that extreme age/size, browser-idle state, AI verdicts and user verdicts cannot restore raw deletion authority.

Normal final-head gate remains mandatory: lock/dependency checks, Ruff, strict mypy, full pytest/current workflow, Windows EXE artifact and CodeQL must all be green before merge.
