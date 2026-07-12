# G6 broker verification protocol (future, blocked)

This protocol is an executable evidence asset for a future signed broker. It does not authorize
broker implementation or cleanup execution before G2, and it cannot pass G6 by itself.

## What the verifier covers

Run `scripts/verify_broker_install.py` from a standard, non-elevated account after installing a
release candidate. The verifier fails closed unless all of these conditions hold:

- the install root is a strict descendant of the OS-reported x64 Program Files known folder;
- the parent, install root, every directory, and every installed artifact deny the current token
  each mutation access right tested (`WRITE_DATA`, append/add-child, extended attributes,
  delete-child, attributes, delete, DACL, and owner changes);
- the exact install tree equals a bounded release manifest, with no symlink, junction, Cloud Files
  boundary, unexpected configuration, external action table, or hard-linked artifact;
- installer, broker, and DLL hashes match a manifest whose hash is supplied independently;
- installer, broker, and every DLL have a valid embedded Authenticode signature from the exact
  publisher certificate thumbprint recorded in the manifest;
- a second tree inventory is identical, including available volume/file identities.

The Authenticode query uses the fixed inbox Windows PowerShell executable and the fixed
`Microsoft.PowerShell.Security` module path. It runs with no profile, a minimal environment,
bounded output/time, and a `KILL_ON_JOB_CLOSE` Job Object.

Example (placeholders are intentional):

```powershell
uv run --frozen python scripts/verify_broker_install.py `
  --install-root 'C:\Program Files\Reclaimer' `
  --installer 'D:\release\ReclaimerSetup.msi' `
  --manifest 'D:\release\broker-install-manifest.json' `
  --manifest-sha256 '<published-64-lowercase-hex>' `
  --output 'G6-artifact-install-observation.json'
```

The output always contains `g6_gate_passed: false`. A `PASS` means only that this artifact/ACL
subgate passed on the current account and machine.

## Remaining mandatory G6 evidence

For every supported Windows 11 build and locale in the release matrix, retain independently
reviewable evidence for:

1. installer elevation and cancellation, uninstall, repair, rollback, and reboot interruption;
2. ordinary-user inability to replace the broker, any DLL, action contract, or journal, tested
   from at least two unrelated standard-user accounts;
3. fixed-version IPC authentication and replay resistance; unknown, repeated, missing, reordered,
   oversized, malformed UTF-8, path-like, and duplicate-enum inputs all rejected;
4. PATH, current-directory, `PYTHONPATH`, `DOTNET_*`, COM, and DLL-search injection attempts having
   no effect on executable/module identity or action selection;
5. UAC cancellation, client disconnect, broker crash, timeout, and machine restart never causing
   an unjournaled continuation or automatic replay of an indeterminate action;
6. the broker exposing no free path/string action, service-control operation, ACL mutation,
   `ResetBase`, raw file deletion, or extensible runtime plugin/action-table surface;
7. a non-author security review of broker, installer, IPC, intent/reconcile, and negative tests.

Only the complete matrix can close G6. Until then there is no broker project or privileged product
path in Reclaimer.

