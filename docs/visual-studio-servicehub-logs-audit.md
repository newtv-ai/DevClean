# Visual Studio ServiceHub log re-audit

Audited again: 2026-08-23

DevClean treats `%TEMP%\servicehub\logs` as known Visual Studio diagnostic
evidence. The directory is visible and technically removable in a documented
troubleshooting workflow, but it is **not** a deterministic cache-cleanup root.

## Why the old TOOL rule was reopened

The earlier audit correctly identified an exact Microsoft-owned path, but it
made a second inference that the source did not support: it converted a
troubleshooting reset instruction into a generic retention lifecycle by adding
a DevClean-specific 14-day idle threshold and 16 MiB minimum reclaim value.

That conflicts with the current DevClean rule model. Age and size can rank a
known cleanup opportunity, but they cannot manufacture deletion authority for
data whose retention value depends on the user.

## Current Microsoft boundary

Current Microsoft Visual Studio performance-troubleshooting guidance describes
ServiceHub and other satellite processes as out-of-process components and uses
`%temp%\servicehub\logs` as diagnostic evidence.

For a **directly reproducible** out-of-process issue, Microsoft instructs users
to start by deleting the `logs` folder, enable full ServiceHub tracing, and then
reproduce the problem. This proves two useful facts:

1. the exact `logs` subtree is understood product-owned diagnostic data rather
   than unknown user content; and
2. the folder can be removed as part of a deliberate diagnostic reset workflow.

The same guidance also says that if the issue **cannot be reproduced**, the
folder should be kept intact. That statement is decisive for generic cleanup:
retention can have real user value, and whether that value still matters cannot
be inferred from filesystem age or logical size.

Official Microsoft reference:

- https://learn.microsoft.com/en-us/visualstudio/ide/how-to-increase-chances-of-performance-issue-being-fixed?view=visualstudio

## Current decision

Exact `%TEMP%\servicehub\logs` is now:

- category: `SYSTEM_LOGS`;
- semantic owner: `USER`;
- scan/catalog policy: `REPORT_ONLY`;
- review action: `USER_DECISION`;
- deterministic whole-tree authority: **none**;
- process requirement for a user-approved mutation: Visual Studio and
  ServiceHub satellite processes must be closed.

A recent, tiny, old, large, or unknown-last-use ServiceHub log tree stays in the
same USER lane. Those properties can help explain or rank the item, but they do
not convert diagnostic evidence into TOOL-owned junk.

## Boundary retained

DevClean does not generalize this rule to:

- `%TEMP%\servicehub` as a whole;
- similarly named sibling directories;
- per-instance Visual Studio state;
- `WebTools`;
- Roslyn siblings outside the exact audited `Roslyn\Cache` root;
- project `.vs`, `bin`, or `obj` content;
- Visual Studio Installer package/instance metadata.

The execution path still rechecks application ownership and process state. A
process-query failure remains fail-closed.

## Revisit condition

ServiceHub logs should return to deterministic TOOL cleanup only if Microsoft
publishes an unconditional product retention/cleanup contract or a supported
maintenance operation whose semantics prove that the selected diagnostic
records are disposable independent of user troubleshooting needs. A new
DevClean age threshold by itself is not such evidence.
