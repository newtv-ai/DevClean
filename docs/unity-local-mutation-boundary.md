# Unity direct-mutation local-volume boundary

Audited: 2026-08-18

## Why this cross-cutting guard exists

Unity supports redirecting or sharing several kinds of storage, and ordinary project directories can also live on mapped/network locations. A source-backed path tells DevClean what the storage means; it does not prove that the current user exclusively owns the storage or that a destructive action should affect every consumer of it.

DevClean's direct filesystem mutation lanes therefore use the same conservative boundary already applied elsewhere in the cleanup engine: **destructive direct mutation is limited to a local fixed volume with no reparse-redirection in the existing ancestor chain**.

## Covered direct-mutation lanes

This guard currently applies to:

- Unity project `Library` deletion;
- individual Unity Asset Store `.unitypackage` deletion;
- deprecated Unity UPM `packages` deletion.

These actions already have narrow source-backed semantics and handle-bound identity checks. The local-volume requirement adds an ownership/isolation gate; it does not replace those existing checks.

## Effect

A Unity source on shared, remote, removable, or reparse-redirected storage can still be inspected and reported. DevClean simply refuses the direct destructive action.

The rule intentionally fails closed at execution time even if a UI inventory was collected earlier. Immediately before mutation, DevClean must still prove the exact source boundary and the local fixed-volume condition.

## Why vendor-managed actions are different

This restriction is specifically for DevClean's own direct filesystem mutations. A future source audit may permit a vendor command/API to operate on a supported shared location if the vendor itself owns the concurrency and storage semantics. Such authority must come from that vendor action, not from a directory name or from DevClean assuming exclusive ownership.

## Non-goals

This policy does not turn shared storage into AI work. The technical identity may be fully known; the operation is simply non-executable through DevClean's direct-mutation path.
