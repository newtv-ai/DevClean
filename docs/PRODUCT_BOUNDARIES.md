# DevClean product boundary

DevClean is a disk-cleaning product, not a generic storage browser and not a collection of manual maintenance dialogs.

## Decision order

1. **Shared/audited rule first.** If DevClean already has a specific rule for an item, the program follows that rule automatically. UI code must not add a second blanket policy such as “high rebuild cost means hide it” or “recent cache means always delete it”. Any such condition belongs in the rule itself.
2. **Unresolved items go to AI review.** Items that local rules genuinely cannot decide remain visible in the review/AI lane. AI may recommend DELETE or KEEP/UNSURE. DELETE can be promoted into the normal cleanup flow; KEEP/UNSURE is left in place.
3. **AI is expensive, so reusable conclusions should become rules.** A conclusion that is safe and general across machines/users should be represented as a reusable rule instead of repeatedly spending AI calls. Local learned rules are the current mechanism; a future shared-rule service must preserve the same safety boundary and provenance.
4. **Protected data is different from unresolved data.** USER-owned data, explicit KEEP rules, protected Windows roots, process guards, reparse points and other hard safety boundaries stay protected. “Unknown” by itself is not a reason to silently disappear from the product; unresolved-but-reviewable items belong in the review/AI lane.

## UX invariant

The normal flow is: **scan -> known rules resolve automatically -> unresolved reviewable items go to AI -> user cleans approved results**. Advanced maintenance tools may remain available for exceptional vendor operations, but a rule-covered cache must not require the user to open a separate tool just to obtain the decision DevClean already knows.

## Engineering rule

Do not change this product boundary while fixing performance, UI, safety checks, or cleanup success rate. Those fixes must preserve the decision order above unless a product change is explicitly requested.
