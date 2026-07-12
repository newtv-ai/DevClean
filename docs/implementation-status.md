# Reclaimer implementation and gate ledger

Checked: 2026-07-12. This ledger separates code that exists in the current tree from optional
public-release and future-expansion evidence. GitHub Actions, ProcMon, Cloud Files,
additional-machine, disposable-VM, code-signing, and reviewer protocols remain useful for a later
public release; they are not prerequisites for the current local scan-and-recycle workflow.

No complete external manifest/matrix in this repository closes G0, G1, G2, G5, or G6. Those
protocols are retained as future verification infrastructure and are not local product features.

## Current safety boundary

Every stored resource remains non-actionable, exported reports are one-way, and stored review plans
remain disabled `REPORT_ONLY` artifacts. Separately, `reclaimer recycle` can move up to 32 exact
filesystem candidate IDs from one completed scan to the Windows Recycle Bin after a typed
confirmation and two identity preflights. It accepts no path/glob/directory/`--yes`, refuses
reparse/Cloud Files/hard-links/protected user assets, and never calls a permanent-delete API.
There is no `apply`, vendor cleanup executor, broker, BleachBit invocation, service-start, or UAC
path.

The `doctor` command may report that its token is elevated, but it does not open inventory/state in
that condition; every other CLI command refuses an elevated token. Failure to determine elevation
is also fail closed. The Windows maintenance surface only prints an official
component-store analysis command for the user to run independently; Reclaimer neither elevates nor
runs or audits that command.

## Phase 0 — repository, schemas, and release assets

Implemented locally:

- Independent GPL-3.0-or-later project skeleton; no BleachBit, Winapp2, Sifty, or InstallerClean
  code/rules are vendored.
- ADR-001 through ADR-003, threat model, coverage matrix, adapter command baseline, evidence policy,
  Resource/ScanReport/InventoryPlan JSON Schemas, lint, strict typing, tests, and coverage policy.
- Locked development environment, dependency audit command, CycloneDX runtime SBOM generation,
  wheel validation, artifact SHA-256 generation, Windows CI workflow, and Python CodeQL workflow.
- `scripts/build_release.ps1` emits `artifacts/release-validation.json` outside the three-file
  release payload. It binds source revision/version, wheel/SBOM/checksum and builder/validator/lock
  hashes, clean-runtime installation, byte-for-byte wheel/SBOM reproducibility under the same build
  inputs, Schema/RECORD validation, and an inventory-only top-level CLI help surface.
- A closed G0 evidence contract now includes the release-readiness protocol, incomplete owner/CI/
  CodeQL and release-manifest templates, mechanical source-boundary auditor, and
  `validate_gate_evidence.py`
  cross-binding for the product artifact, owner decision, source audit, dependency audit,
  `release-validation.json`, and real CI/CodeQL attestations.
- The source auditor, release validator, and G0 evidence validator all enforce the canonical full
  GPLv3 UTF-8/LF byte contract; a synchronized repository/wheel summary cannot pass as the license
  text. Release-shaped source revisions additionally require a matching clean Git checkout.

Open G0 evidence — G0 is not passed:

- The project owner must make the final license decision before the first public release.
- The configured `windows-latest` Python 3.11–3.13 matrix, dependency audit, build/SBOM job, and
  CodeQL workflow must run in the real GitHub repository. Local success is not CI evidence.
- A local `release-validation.json` with `result=PASS` is only mechanical build evidence. It does
  not prove the project-owner license decision, source originality, real GitHub execution, CodeQL
  state, or release authorization, and it cannot close G0 by itself.

## Phase 1 — read-only Windows core

Implemented locally:

- Versioned Resource/Action/Plan models with fail-closed provenance, risk, undo, reconstruction,
  confidence, and non-executable semantics.
- Runtime model bounds mirror the checked-in Schemas. The current Plan rejects every non-
  `REPORT_ONLY` action; adapter results and the state store independently reject
  `actionable=true` resources.
- SQLite schema migrations, `synchronous=FULL`, foreign keys, atomic bounded batch writes,
  integrity checks, and paged/streamed report reads.
- Non-elevated `doctor`, local-root `scan`, and one-way JSON/Markdown `report` commands.
- Handle-based Windows file identity and allocation size; bounded hard-link accounting.
- Iterative traversal, cooperative cancellation/progress, per-item permission errors, reparse
  boundaries, and Cloud Files no-recall metadata flags.
- Default path/credential redaction, explicit full-path opt-in, and terminal/Markdown control and
  bidi character sanitization.
- State database parent directories and scan-scoped evidence roots are restricted to a fixed local
  volume without reparse ancestors. On Windows they receive a protected DACL with inheritable full
  control only for the current token SID, LocalSystem, and Builtin Administrators, followed by an
  exact-policy audit. The database file itself receives a separate protected, non-inheritable DACL,
  rejects multiple hard links, and is re-secured before use; migration backups receive the same
  file policy. On non-Windows test platforms the fallbacks are owner-only `0700` for directories
  and `0600` for files.

Important boundary: Reclaimer applies this private-directory policy to its state/evidence roots,
not to an arbitrary path selected with `report --output`. An exported report may contain sensitive
data, especially with `--full-paths`, and inherits the destination's policy. Export is limited to a
new file on a fixed local path without reparse ancestors, refuses overwrite/symlink targets, and
publishes only after a same-directory temporary file is fully flushed.

Retained local evidence on Windows 11 build 26200, Python 3.13.13:

- Local unit/integration, Ruff, strict Mypy, and branch-coverage runs have passed; they still need
  the configured GitHub matrix result before release.
- Isolated local test environments for Python 3.11, 3.12, and 3.13 pass on this Windows host;
  these runs are compatibility evidence only and are not immutable GitHub G0 evidence.
- A real CLI smoke scanned the repository test tree, exported a streamed JSON report, and confirmed
  `safety_boundary.executable=false`.
- The one-million-resource benchmark stored and integrity-checked all 1,000,000 rows in
  512-resource batches against the current local wheel in 329.301 seconds. Peak Python-traced
  memory was 1,267,137 bytes and peak working set was 32,501,760 bytes; the result remains
  explicitly bound to `WORKTREE_UNCOMMITTED`, and retained metadata is in
  `docs/evidence/benchmarks/2026-07-12-streaming-million.json`.
- A deterministic slow-metadata fixture requests cancellation during an in-flight observation and
  asserts that no subsequent metadata call starts and the scanner closes in under two seconds.
- Current-host disposable fixtures pass for a junction boundary, an intentional junction loop
  bounded at its first reparse edge, sparse allocation accounting, NTFS compression metadata,
  and a long path containing Chinese text and spaces. They improve
  local coverage but are not revision-bound G1 physical-manifest evidence.

Open G1 evidence — G1 is not passed:

- The checked-in G1 physical-boundary protocol, intentionally incomplete manifest template, gate
  schema, and manifest/matrix validator make future evidence machine-checkable. They do not supply
  the missing physical observations.
- Record real mount-point, loop-junction, OneDrive Files On-Demand, ReFS, removable-volume, and
  access-denied fixtures on supported Windows builds without hydration or traversal.
- Complete the GitHub Actions matrix and required physical-machine/disposable-VM smoke evidence.
- Until those fixtures are recorded, unit tests and the synthetic million-row benchmark are only
  partial G1 evidence.

## Phase 2 — nine read-only adapters and evidence containment

Implemented locally:

- Probe/inventory adapters for Hugging Face, pip, uv, Conda, npm, pnpm, Docker, Ollama, and VS Code.
  They are explicitly registered and do not create actionable resources.
- Hugging Face uses version-gated strict JSON shapes; pip inventories explicit interpreters; uv
  gates the supported command surface; Conda uses category-specific JSON dry-runs; npm launches
  paired `node.exe` + `npm-cli.js`; pnpm and VS Code avoid their CLIs and use bounded filesystem
  inventory instead.
- Docker only queries a daemon that is already online through the fixed local named pipe, using an
  empty Reclaimer configuration sandbox. Its known sandbox writes are classified as
  `OBSERVATION_WITH_OPERATIONAL_WRITES`; Docker Desktop is never started.
- Ollama has no CLI path. It only permits bounded `GET` requests to
  `127.0.0.1:11434/api/version|tags|ps`, without proxy inheritance or redirects, and never starts,
  loads, or pulls a model.
- Every Windows vendor process is assigned to a per-invocation Job Object configured with
  `KILL_ON_JOB_CLOSE`. Job creation/configuration/assignment failure rejects the query; normal
  completion, timeout, output limit, and interruption clean up the assigned process tree. A small
  `Popen`-to-assignment launch window remains documented and is not treated as closed.
- A command executable must pass two independent launch-boundary checks as a resolved, ordinary,
  non-cloud local `.exe`; batch files, reparse paths, network paths, and non-`.exe` files are
  rejected before `Popen`. It is then hashed and identity-checked before the query and observed
  again after it. Size, mtime, SHA-256, and available volume/file-ID fields must match or evidence
  publication fails. This is replacement detection, not Authenticode publisher verification.
- Original command stdout/stderr and Ollama response bytes remain memory-only. Persistence receives
  only conservatively redacted UTF-8 or a deterministic withholding marker; source and stored
  sizes/SHA-256 values are tracked separately. Adapter runs and command/loopback evidence are stored
  in SQLite and exported through the report schema.
- Resource `evidence:<id>` links and adapter-run `evidence_ids` are checked against the same scan
  when written and again in a bounded streaming integrity pass before any report byte is emitted.
  This supplies the cross-array foreign-key invariant that JSON Schema cannot express.
- The same pre-output pass verifies resource, adapter-run, and evidence JSON identities against
  their indexed SQLite columns, rejects non-observational effect classes, and rechecks the fixed
  Ollama loopback endpoint. Tampered critical fields fail before a streaming report emits bytes.

Retained partial G2 smoke:

- A nine-adapter local smoke completed on Windows 11 build 26200. The redacted report passed JSON
  Schema, persisted nine adapter-run records and 18 evidence records, and all eight emitted
  resources remained non-actionable. The retained summary is
  `docs/evidence/smoke/2026-07-11-local-adapters.json`.
- The run was one-machine evidence with no ProcMon trace. Conda was absent; Docker and Ollama were
  offline and were not started; no real VS Code extension root was present. Those skips are safe
  behavior but are not product-validation evidence.

Open G2 evidence — G2 is not passed and cleanup implementation remains blocked:

- The conservative ProcMon CSV validator, service-state before/after collector, closed machine-
  manifest contract, scan-report/product/prerequisite cross-checks, and 2-physical +
  1-disposable-VM matrix
  validator are implemented. Synthetic CSV/manifests only test those validators; no real G2 capture
  or matrix is recorded.
- Capture ProcMon or equivalent evidence proving that managed cache roots and user assets receive
  no writes; explicitly account for any approved Reclaimer sandbox/query-log writes.
- Complete smoke on 2 physical development machines and 1 disposable VM, including installed
  Conda, pre-existing online Docker/Ollama, and a real supported VS Code extension root.
- Complete the required version/transcript matrix for products whose real supported versions are
  not yet represented; unavailable products and skipped cases cannot be counted as coverage.
- No cleanup action code may be added merely because the adapters are implemented locally.

## Current Phase 3 surface — report-only plans, not G3

Implemented locally:

- `plan create` selects between 1 and 256 exact opaque candidate IDs from a completed local scan;
  `plan show` reads the resulting record back from SQLite.
- The model, schema, storage reader, and CLI require every action to be `REPORT_ONLY`,
  `PURE_QUERY`, `selection_mode=NONE`, `preview_mode=NONE`, `reclaim_scope=UNKNOWN`, and
  `enabled=false`; the plan declares `executable=false`.
- Storage retrieval rejects a plan that violates this inventory-only boundary. No plan import,
  apply command, executor, durable action intent, reconcile loop, or vendor action exists.

This narrow review surface is schema/policy groundwork. It does not satisfy G3 or G4, and it does
not weaken the rule that the first cleanup-action code is blocked on G2.

## Later security gates

G3 and G4 are not passed: executable vendor plans, preflight, durable intent/reconcile, crash
recovery, confirmation UI, and vendor cleanup actions do not exist.

G5 is not passed: a future race/review protocol, intentionally incomplete template, closed evidence
contract, and validator for G2/G3/G4 prerequisites, fixed-NTFS disposable-VM execution, race reports,
canaries, static audit, and non-author review now exist. There is still no `DIRECT_FS_ACTION`
executor, approved cache root, junction/rename/file-ID/lock/read-only/hard-link/new-file
10,000-iteration race evidence, or independent non-author review. Inventory file identities are
evidence; they are not deletion authority.

G6 is not passed: there is no broker project, installer, IPC protocol, broker/journal ACL model,
Authenticode signing, certificate, DLL-search hardening, UAC workflow, or supported-Windows
machine matrix. A read-only future-G6 verifier and protocol can check one installed Program Files
tree's closed manifest, hashes, signatures, file identities, and standard-user mutation denial; its
output hard-codes `g6_gate_passed=false` because it does not cover broker behavior or the required
matrix. No verification asset or design statement should be read as implemented privilege
separation.

The permitted next work is read-only verification, evidence tooling, documentation, schemas, and
non-executable scaffolding. Cleanup execution, direct filesystem actions, broker actions, or
BleachBit `--clean` remain prohibited until their prerequisites and specific gates are recorded as
passed with the required external evidence.
