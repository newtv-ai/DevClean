# Editor, JetBrains and Python family reconciliation — 2026-08

## Scope

This current-main reconciliation closes three second-pass family rows whose product-specific source audits, implementation boundaries and regression tests already landed. It does not create new cleanup authority. Generic names, age, size, apparent regenerability and one-off AI/user guesses remain insufficient to authorize deletion.

## Electron/editors

The current tree already uses product-specific storage semantics rather than treating all Code-OSS/Electron products as interchangeable.

- Codex established the original application semantic boundary inherited by the later Claude profile; persistent conversation/auth/config state is protected while only source-identified regenerable state can be delegated.
- Claude Code: #2 and #6 separate transcripts/history/usage and authored/plugin/background state from narrow regenerable caches/runtime state; plugin maintenance is vendor-command based and whole config roots never become delete roots.
- Cursor: #3 and #7 keep chat databases, recovery copies, workspace/history/checkpoints and installed extensions outside raw cleanup while retaining only exact audited Electron/cache lanes.
- VS Code: #4 covers Stable/Insiders, portable and explicit roots, protects Backups/workspaceStorage/History/globalStorage/extensions and grants whole-tree authority only to exact audited cache/log/crash/tmp subtrees.
- Trae: #5 is default-deny for proprietary/user/AI state and only delegates well-understood Electron runtime caches/logs/crash data.
- Windsurf: #9 keeps Cascade conversations, memories, plans, workspace/history and authored MCP/rules/workflows/skills/hooks/extensions persistent while allowing only proven Code-OSS/Electron caches.

Conclusion: the editor family is re-verified. Shared Electron ancestry is classification evidence only; every product retains its own USER/KEEP ceiling and process/root guards. Unknown proprietary state remains protected.

## JetBrains / Toolbox / Android Studio

- JetBrains IDEs: #21 separates configuration/plugins and mixed system state (including Local History/JCEF/VFS) from exact regenerable `index`, `tmp`, `vcs-log` and log roots.
- JetBrains Toolbox: #22 keeps the mixed Toolbox application/settings root protected and limits positive raw cache authority to exact documented download/temp/log subtrees with independent process guards.
- Android Studio: #23 intentionally stays separate from the JetBrains IDE profile, reusing only source-proven IntelliJ-platform subtree semantics while protecting Google-owned config/plugins/system/SDK/AVD/project state.
- Old JetBrains versions: #120 adds a much narrower vendor-expired default system-directory lane based on current IntelliJ Platform 180-day automatic-clean semantics, with installation/process/local-fixed/identity checks and no config/plugin deletion.
- #148 additionally preserves JetBrains-managed JDK `lib/modules` as audited product KEEP knowledge rather than treating a large runtime file as disposable.

Conclusion: the family is re-verified. Complete IDE/system/toolbox roots never inherit generic deletion authority; positive mutation is exact source-owned cache or vendor-expiration scope only.

## Python ecosystem

- pip: #29 established source-audited cache identity; #56 removed raw whole-cache authority and routes mutation through exact-root-confirmed `pip cache purge`.
- uv: #31 protects cache/persistent/config/managed-Python state from raw deletion; #58 exposes only exact-root-confirmed vendor `uv cache prune`, preserving uv's own safe periodic lifecycle.
- Conda: #32 protects package caches/environments/base/config and forbids broad raw deletion; #60 limits maintenance to exact-root-confirmed `conda clean --tarballs --index-cache`, deliberately excluding risky package/all/force modes.
- Hugging Face Hub: #37 made HF_HOME/token/Xet/assets and Hub storage default-protected; #132 upgrades only exact vendor-inventoried repo/revision/prune actions to USER_REVIEW with exact cache root, executable identity, dry-run and complete-inventory checks.
- PyTorch Hub: #133 remains REPORT_ONLY because current PyTorch source lacks a public complete exact inventory/remove/prune contract and cache directory/checkpoint provenance is not reliably reversible.

Conclusion: the Python family is re-verified. Positive maintenance uses vendor-owned inventory/mutation contracts where exact scope is provable; mixed stores, environments, credentials, model/checkpoint state and ambiguous provenance remain protected/report-only.

## Cross-family invariants retained

- Application KEEP/USER semantics outrank learned generic rules.
- Learned/default reusable rules remain file-only and cannot create whole-directory authority.
- Direct filesystem mutation remains local-fixed and exact-identity/reparse/hardlink/concurrency guarded.
- Vendor commands are used only after exact executable/root/state revalidation and never as broad shell/project execution fallbacks.
- Age/size are benefit evidence only unless the vendor itself defines the lifecycle boundary.

## Revisit triggers

Reopen a row only for a concrete upstream storage/API/lifecycle change or a verified current-main regression. Do not reopen merely to increase raw-delete coverage or to infer safety from common cache names.