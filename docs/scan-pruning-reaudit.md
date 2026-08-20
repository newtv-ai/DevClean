# Scan exclusion and pruning re-audit

Audited: 2026-08-20

## Finding

DevClean intentionally scans the user profile plus source-specific roots rather than whole drives. The profile walk also prunes high-cost or user-owned directory names such as `Documents`, `.git`, installed payload trees and system areas.

The previous outermost-root deduplication could silently erase an explicit nested root. For example, an additional `Documents\project` root was dropped because `%USERPROFILE%` already contained it, but the profile traversal then pruned `Documents`; the requested project was never visited. The same shape could hide an application-audited root below a skipped-name ancestor.

## Correction

- broad roots are still deduplicated and broad skipped-name subtrees remain pruned;
- explicit user `additional_paths` and runtime roots carrying an attached audited application rule are treated as specific roots;
- a specific nested root is re-added only when every broader retained root would encounter a configured skipped-name component before reaching it;
- an exact specific root may bypass its own basename in the skip-name set, but descendant name pruning remains active;
- exact `excluded_paths` remain authoritative and beat every specific-root override;
- static/report-only catalog roots do not receive this special reachability treatment merely because their names are familiar.

This preserves scan performance and user-content protection while making explicit/source-audited reachability truthful.
