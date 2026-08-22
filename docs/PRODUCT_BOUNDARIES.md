# DevClean product boundary

DevClean is a disk-cleaning product, not a generic storage browser and not a collection of manual maintenance dialogs.

## Decision order

1. **Shared/audited rule first.** If DevClean already has a specific rule for an item, the program follows that rule automatically. UI code must not add a second blanket policy such as “high rebuild cost means hide it” or “recent cache means always delete it”. Any such condition belongs in the rule itself.
2. **Unresolved items go to AI review.** Items that local rules genuinely cannot decide remain visible in the review/AI lane. AI may recommend DELETE or KEEP/UNSURE. DELETE can be promoted into the normal cleanup flow; KEEP/UNSURE is left in place.
3. **AI is expensive, so reusable conclusions should become rules.** A conclusion that is safe and general across machines/users should be represented as a reusable rule instead of repeatedly spending AI calls. Local learned rules are the current mechanism; a shared-rule service should distribute only conclusions with explicit provenance and the same safety boundary.
4. **Protected data is different from unresolved data.** USER-owned data, explicit KEEP rules, protected Windows roots, process guards, reparse points and other hard safety boundaries stay protected. “Unknown” by itself is not a reason to silently disappear from the product; unresolved-but-reviewable items belong in the review/AI lane.

## Scan invariant

There is **one normal scan**, not a smart/deep mode choice exposed to the user.

- A folder already covered by an audited whole-tree rule is treated as one cleanup object. DevClean may collect lightweight aggregate metadata needed by that rule, but it must not run the full per-file classifier over every child.
- A folder whose contents still need a decision is scanned file-by-file. Local rules resolve what they can; residual reviewable ambiguity goes to the AI lane.
- Performance work may reduce traversal and classification cost, but it may not replace or weaken the rule that decides whether the item is deletable.

## UX invariant

The normal flow is: **scan -> known rules resolve automatically -> unresolved reviewable items go to AI -> user cleans approved results**.

There is **no user-facing Tool Center and no per-application maintenance checklist**. If a package manager, IDE, browser, model store, build tool, Windows component, WSL cache, or other source has a rule that DevClean can evaluate safely, that rule belongs in the normal scan/decision engine. A user must not be asked to open a separate dialog and rediscover the same storage manually.

A maintenance capability that cannot yet be represented safely in the normal decision engine must stay non-destructive until it is integrated; it must not be used as an excuse to reintroduce a manual tool palette.

## Engineering rule

Do not change this product boundary while fixing performance, UI, safety checks, or cleanup success rate. Those fixes must preserve the decision order above unless a product change is explicitly requested.
