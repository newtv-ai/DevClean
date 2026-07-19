# DevClean security policy

DevClean can move and permanently remove user files. Treat data-loss, path-boundary and authorization defects as security issues.

## Supported versions

Only the newest source revision and explicitly published pre-release are supported. `0.2.0a1` remains Pre-Alpha until the release evidence named in `docs/release-engineering.md` is regenerated and approved.

## Private reporting

Use the repository Security tab and **Report a vulnerability** (GitHub private
security advisory). Private vulnerability reporting must be enabled before this
repository is made public. If the button is unavailable, do not disclose an
exploit, sensitive path, credential, or reproducible data-loss primitive in a
public issue; open only a non-sensitive issue asking the maintainer to enable the
private channel.

Include the revision/version, Windows and filesystem details, affected path class, minimal reproduction, observed mutation, expected refusal and whether the test used disposable data. Do not test against another person's machine or valuable files.

We aim to acknowledge a report within 7 days, provide a triage decision within 14 days and coordinate disclosure after a fix and affected-artifact notice are ready. These are targets, not a warranty.

## Current invariants

- Scanning, classification, duplicate analysis and AI import never mutate scanned objects.
- A completed scan starts with zero cleanup selection.
- AI output is inert advice; it cannot add paths, choose an irreversible mode or create execution authority.
- Execution accepts exact file records from the current completed scan, not directories, globs, scripts or model-provided paths.
- Protected/system/report-only items, reparse points, Cloud Files placeholders, hard links and incomplete identities are refused.
- The ordinary GUI refuses elevation.
- Final execution revalidates approved-root identity, final handle path, 128-bit file identity, size, timestamps, attributes and link count.
- The target mutation handle shares neither WRITE nor DELETE. Directory handles allow child-entry writes but omit DELETE sharing so the pinned directory object cannot be replaced.
- A quarantine directory is a unique direct child of the pinned approved root and receives its protected DACL atomically at creation. A pre-existing namespace is never adopted.
- Recoverable quarantine preserves the source file DACL; restore never overwrites an occupied original path.
- Permanent cleanup follows source → exact quarantine → durable `PURGE_PENDING` → handle-bound purge. It never falls back to pathname deletion or recursive removal.
- Every low-level batch records all intents in SQLite before the first target mutation. Ambiguous states are visible and never automatically replayed.
- Incremental notifications are invalidation hints only. Overflow, token/root mismatch or other uncertainty causes a full traversal.

## Withdrawn pre-release builds

Commit `aba14a25486d4a9e4b0d7c144d5e08d9873516ba` and Windows executable SHA-256 `84b0ae919bc9da796126e8ed49f74df9c856cff5d42b650ff8c11e97e1b251ac` are withdrawn. That GUI permanently deleted some old Temp and CrashDumps files while scanning and included an AI-driven permanent-delete path.

Wheel SHA-256 `6d9f6a2f32eb5534e0a725f06d25f066ac285b4ac6ea46c651c3562d9a93c5b0` is also withdrawn because it exposed a cleanup command inconsistent with its inventory-only release evidence.

Do not execute or distribute these artifacts. Disabled local copies are retained under `artifacts/quarantine` solely for audit evidence.

Local history recorded at least 135 successful `AUTO_PERMANENT` operations between 2026-07-12 17:29 and 17:33 China Standard Time, totaling 217,980,912 logical bytes. This is a lower bound because the affected build wrote history only after successful deletion and ignored history-write failures. Evidence and limitations are recorded in `docs/evidence/incidents/2026-07-12-scan-time-deletion-summary.json`.

## Out of scope for a public issue

General cleanup suggestions, false-positive classification without an execution path, UI preferences and support questions are ordinary issues. Any way to mutate during scan/AI import, escape an approved root, replace a target, bypass confirmation, replay an ambiguous action, weaken quarantine privacy, or misreport a permanent purge belongs in a private security report.
