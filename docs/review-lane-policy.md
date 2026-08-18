# DevClean review-lane policy

Audited: 2026-08-18

DevClean is a disk-cleanup tool, so local deterministic knowledge should do as much work as it safely can. AI is a fallback for ambiguity, not the default reviewer.

## Product rule

Every discovered item belongs to one of four semantic outcomes:

1. **Tool-determined cleanup** (`DETERMINISTIC_CANDIDATE`) — DevClean has an audited, broadly valid reason that the item is disposable or reproducible. It is shown in **可以删除**, selected by default, and still requires the user's explicit cleanup action.
2. **User review** (`USER_REVIEW`) — DevClean understands the type of item but cannot claim that deleting it benefits every user. It is shown in **你来决定** and does not consume AI by default. If the user is also unsure about an individual file, they may explicitly select it and escalate that file to AI review.
3. **AI review** (`AI_REVIEW`) — local evidence is genuinely insufficient to identify the item or determine whether removal is appropriate. These files form the default AI export set.
4. **Protected/report-only** (`REPORT_ONLY`) — known persistent state, installed payloads, system-managed data, or other content for which generic deletion authority is not appropriate.

## What qualifies as tool-determined

A low-cost local decision needs a narrow, reproducible boundary. Examples include an exact audited application cache rule, an exact vendor-managed cleanup root, or an approved aged-temp policy. Process guards, exact-file identity checks, KEEP precedence, local-fixed-volume checks, and whole-tree revalidation still apply at execution time.

A generic filename suffix, a directory merely named `cache`, or a broad build-output name is not enough to make a universal deletion claim. Those signals can reduce uncertainty, but they belong in user review unless stronger application/vendor evidence exists.

## User review versus AI

User review is for cases where the product can explain the tradeoff in ordinary terms: for example, a generic cache-looking path, a project output that can usually be rebuilt, or a manually reviewed known root. The user can mark the item reusable/deleteable without paying for AI, or explicitly escalate a selected file if they want a second opinion.

AI review is reserved by default for the remainder: unknown files, ambiguous development-storage hints, and heuristic categories where path metadata alone cannot support a stable local decision. AI advice never grants execution authority; imported DELETE decisions still require the normal DevClean cleanup path and safety checks.

## Cost principle

The order is intentional: **deterministic local rules first, user judgment second, AI only for residual ambiguity or explicit escalation**. Every new source audit should try to move stable cases leftward without weakening the universal-safety bar.
