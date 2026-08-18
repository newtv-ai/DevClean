# Built-in cleanup-root authority audit

Audited: 2026-08-18

This audit applies the DevClean review-lane rule to the stock `scan-rules.json`: a configured path is not automatically a universal cleanup target merely because its name looks temporary or cache-like.

## Authority changes

| Root | Previous policy | Audited policy | Reason |
| --- | --- | --- | --- |
| Windows crash / WER roots | `VENDOR_MANAGED` | `AGE_BASED_REVIEW` | Crash dumps are disposable diagnostic artifacts, but recent evidence can still matter. Old entries can use the normal age threshold; recent entries stay with the user. |
| `%SYSTEMROOT%\Prefetch`, `%SYSTEMROOT%\Logs`, `%SYSTEMROOT%\CbsTemp` | `VENDOR_MANAGED` | `MANUAL_REVIEW` | These mixed Windows maintenance locations do not have one source-backed raw-deletion contract that benefits every user. |
| `%SYSTEMROOT%\SoftwareDistribution\Download` | `VENDOR_MANAGED` | `MANUAL_REVIEW` | Raw deletion can interact with Windows Update servicing state. DevClean should add a service-aware / OS-supported action before promoting it again. |
| `%SYSTEMDRIVE%\Windows.old` | `VENDOR_MANAGED` + whole-root delete | `MANUAL_REVIEW`, no whole-root authority | Microsoft says removing the previous Windows version removes the ability to go back and cannot be undone. That is a user tradeoff, not universal cleanup. |
| `%USERPROFILE%\.lmstudio\models` | `VENDOR_MANAGED` | `MANUAL_REVIEW` | LM Studio documents this as the local downloaded/imported model directory. Imported files can be moved, copied, hard-linked, or symlinked into it, so the tree is user-selected model content rather than disposable cache. |
| broad IDE working-cache roots | `VENDOR_MANAGED` | `MANUAL_REVIEW` | The grouped rule mixed unrelated products (`skylot`, TypeScript, Dart analysis state) without one shared deletion contract. Exact children can be promoted later after separate audits. |
| `%USERPROFILE%\.cache` plus Puro/NVIDIA grouped roots | `VENDOR_MANAGED` | `MANUAL_REVIEW` | A generic `.cache` parent can contain unrelated applications. Naming alone is not enough for universal deletion authority; exact audited descendants still retain their application semantics. |

## Primary sources

Microsoft Support, **Delete your previous version of Windows**:

- https://support.microsoft.com/en-us/windows/deployment/install-upgrade/delete-your-previous-version-of-windows
- Microsoft states that `Windows.old` enables rollback to the previous Windows version and that deleting it cannot be undone.

LM Studio, **Import Models** and **lms import**:

- https://lmstudio.ai/docs/app/advanced/import-model
- https://lmstudio.ai/docs/cli/local-models/import
- LM Studio places model files in `~/.lmstudio/models`; `lms import` can move, copy, hard-link, or symbolic-link a user's local model into that directory.

LM Studio, **lms ls** / **lms get**:

- https://lmstudio.ai/docs/cli/local-models/ls
- https://lmstudio.ai/docs/cli/local-models/get
- These commands describe the model directory as downloaded local models, often many gigabytes in size, not as a transient cache.

## Product consequence

The stock configuration now follows the same cheap-to-expensive order as application profiles:

**audited deterministic cleanup -> user review -> AI only for residual ambiguity or explicit escalation**.

Downgrading a broad stock root does not make DevClean less useful. It removes unsupported certainty. Follow-up source audits should split those broad roots into exact children and promote only the children with a stable rebuild / vendor-cleanup contract.
