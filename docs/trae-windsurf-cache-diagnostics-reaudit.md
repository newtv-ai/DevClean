# Trae / Windsurf cache and diagnostics re-audit — 2026-08

## Why these rows were reopened

This is a current-main semantics correction after #170, not a name-based
expansion of cleanup authority.

The original Trae (#5) and Windsurf (#9) application audits already separated
known VS Code/Electron generated cache subtrees from workspace/history/recovery,
installed-extension, authored AI/Cascade, and unknown persistent state. Their
per-path evaluators still allowed age, minimum reclaim size, or unknown
last-use metadata to turn exact TOOL-owned generated caches into
`TOOL_KEEP_RECENT`, `TOOL_KEEP_LOW_BENEFIT`, or `TOOL_KEEP_UNKNOWN_USAGE`.

That contradicts the current product invariant: exact source-audited whole-tree
TOOL identity is the safety decision; age/size are benefit and ranking facts.

The neighboring `logs` and `Crashpad` rules were deliberately re-audited rather
than automatically inheriting the cache correction.

## Generated cache conclusion

The following already-audited generated-cache classes retain TOOL ownership.
When the owning editor is closed, they stay `TOOL_DELETE` even when fresh, tiny,
or missing last-use metadata.

### Trae

- `Cache`
- `CachedData`
- `Code Cache`
- `GPUCache`
- `DawnCache`
- `CachedExtensions`
- `CachedExtensionVSIXs`

### Windsurf

- `Cache`
- `CachedData`
- `CachedConfigurations`
- `CachedProfilesData`
- `CachedExtensions`
- `Code Cache`
- `GPUCache`
- `DawnCache`
- `GrShaderCache`
- `ShaderCache`
- `Service Worker/ScriptCache`
- `CachedExtensionVSIXs`

The live process guard remains a hard execution condition. Existing neighboring
USER/KEEP state is unchanged.

## Trae logs: USER_REVIEW, not automatic cleanup

A current official TRAE Chinese community support thread from August 2026 covers
very large files under `AppData\\Roaming\\Trae CN\\logs`. TRAE technical support
states that the log information can be deleted, and another support reply says
users can delete log files they no longer need.

Source:

- https://forum.trae.cn/t/topic/175583
- https://forum.trae.cn/t/topic/174996

This establishes a technically understood manual-removal lane, but the wording
is user-retention dependent: **unneeded** diagnostic logs can be removed. It does
not establish that every user's current logs should be automatically discarded
or that an arbitrary seven-day threshold expresses vendor policy.

Therefore `trae-logs` moves from TOOL to USER:

- shown in the user-decision lane;
- no whole-tree deterministic delete authority;
- no age/size rule can turn it back into TOOL;
- if the user selects it, Trae must still be closed at execution time.

This is exactly the review-lane meaning of USER: the product understands what
the data is and can explain the retention tradeoff, but cannot claim deletion
benefits every user.

## Windsurf logs: USER_REVIEW diagnostic evidence

Current Windsurf troubleshooting documentation says retrieving IDE logs is the
first troubleshooting step and instructs users to use `Download Windsurf Logs`
or `Download Diagnostics`, then attach the exported logs to a support ticket.
The common-issues guide likewise tells users how to download diagnostics for
Windsurf support.

Sources:

- https://docs.windsurf.com/troubleshooting/windsurf-gathering-logs
- https://docs.windsurf.com/troubleshooting/windsurf-common-issues

No current Windsurf contract was found granting raw recursive deletion of the
active `logs` root after seven days or any other arbitrary filesystem age.
However the files are known plain diagnostic logs rather than unknown product
state, so the correct lane is USER rather than AI.

Therefore `windsurf-logs` moves from TOOL to USER, with a process-close guard
for any user-approved mutation.

## Crashpad reports and pending state: KEEP / report-only

Trae and Windsurf both use Crashpad report trees. Their former rules granted
raw TOOL whole-tree authority to `Crashpad/reports` and `Crashpad/pending` after
one day. The current shared-component audit does not support that contract.

The repository's Vivaldi Crashpad authority audit records current upstream
Crashpad behavior: Crashpad manages report lifecycle as one
`CrashReportDatabase`, enumerates report records, and removes selected records
through exact report UUID/database operations. Current upstream defaults are
not a generic one-day recursive filesystem rule, and applications can configure
report behavior.

See:

- `docs/vivaldi-crashpad-authority-audit.md`
- https://chromium.googlesource.com/crashpad/crashpad/

The product-specific conclusion is intentionally narrow: the common Crashpad
component proves why raw directory-age deletion is not a valid substitute for
its database lifecycle. It does **not** grant Trae or Windsurf any Vivaldi
browser policy.

Accordingly:

- `trae-crashpad-reports` -> KEEP
- `trae-crashpad-pending` -> KEEP
- `windsurf-crashpad-reports` -> KEEP
- `windsurf-crashpad-pending` -> KEEP
- no whole-tree raw delete authority remains for those roots;
- age, size, AI verdicts, or user verdicts cannot recreate generic Crashpad
  directory deletion through these application rules.

A future positive Crashpad maintenance lane would need exact database/report
identity, process/writer coordination, fresh revalidation, and deletion through
Crashpad/vendor lifecycle rather than recursive directory removal.

## Retained boundaries

Unchanged Trae USER/KEEP examples include:

- `User/workspaceStorage` and `User/History` -> USER;
- `User/globalStorage`, `Backups`, installed extensions, home/config and unknown
  proprietary state -> KEEP.

Unchanged Windsurf USER/KEEP examples include:

- Service Worker `CacheStorage`, workspace/history, Cascade conversations,
  memories and plans -> USER;
- Service Worker persistent state, global user state, backups, authored rules,
  MCP/hooks/workflows/skills, installed extensions, system policy and unknown
  editor/config state -> KEEP.

No generic Electron naming rule is added. No parent data root gains deletion
authority.

## Validation gate

Regression coverage must prove:

- fresh/tiny/unknown-last-use generated caches remain TOOL_DELETE when closed;
- live editor processes still block TOOL cache execution;
- logs are USER_DECISION and user-approved mutation still requires the editor
  closed;
- Crashpad completed and pending reports are KEEP_PROTECTED regardless of age or
  size;
- logs/Crashpad are absent from deterministic whole-tree/catalog delete roots;
- all existing authored/recovery/persistent boundaries remain unchanged.

Final-head lock/dependency checks, Ruff, strict mypy, full pytest/current
workflow, Windows EXE artifact, and CodeQL must be green before merge.
