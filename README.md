# Reclaimer

Reclaimer is an evidence-first, fail-closed disk inventory tool for Windows developer and
AI workstations.

## Current status

**Pre-alpha, inventory only.** The current milestone does not delete files, run maintenance,
request elevation, start vendor services, or invoke BleachBit cleaning.

The first supported execution platform will be Windows 11 x64 versions that are still serviced
by Microsoft at release time. Windows 10 is best-effort inventory only.

## Safety boundary

- Arbitrary directory scan results are report-only.
- Reparse points and Cloud Files placeholders are boundaries and are not traversed.
- Access-denied paths are reported; Reclaimer does not elevate to rescan them.
- Vendor tools are not installed, upgraded, or started by scans.
- AI is not part of the runtime or decision path.

See `docs/threat-model.md` and `docs/adr/` before contributing behavior that changes data.

## Development

```powershell
uv sync --dev
uv run pytest
uv run ruff check .
uv run mypy src
uv run reclaimer doctor
```

## Inventory CLI

Run the main process from a normal, non-administrator terminal. Reclaimer exits if its token is
elevated or if elevation cannot be verified.

```powershell
# Stream a local tree into the per-user SQLite state database.
uv run reclaimer scan --root "$HOME\source" --json

# Export the latest report with user-profile paths redacted.
uv run reclaimer report --latest --format markdown

# The console format is the same streamed, sanitized human-readable view.
uv run reclaimer report --latest --format console

# Full paths require an explicit privacy opt-in; credential-shaped text is still redacted.
# Output must be a new file on a fixed local path; Reclaimer never overwrites an existing target.
uv run reclaimer report --latest --format json --full-paths --output report.json

# Run selected built-in inventory adapters. Offline services are reported, never started.
uv run reclaimer scan --adapter huggingface --adapter pip --adapter uv --adapter npm `
  --adapter pnpm --adapter docker --adapter ollama --adapter vscode --json

# Create an internal review plan from opaque IDs. Every action remains REPORT_ONLY/disabled.
uv run reclaimer plan create <scan-id> --select <candidate-id> --json
uv run reclaimer plan show <plan-id> --json
```

Only local roots are accepted. Reparse points and Cloud Files placeholders are recorded as
boundaries, inaccessible objects are reported, and every arbitrary filesystem resource is
`UNKNOWN`/`RED`/`actionable=false`. JSON and Markdown exports are streamed from SQLite so large
reports do not require an in-memory resource list.

`reclaimer guides` can print a copyable official DISM analysis command. It never launches the
command, requests UAC, or treats the external result as an audited Reclaimer action.

Built-in Phase 2 inventory adapters cover Hugging Face, pip, uv, Conda, npm, pnpm, Docker,
Ollama, and official VS Code user-extension roots. Vendor command output is parsed in memory;
persisted transcripts are redacted UTF-8 or withholding markers. On Windows, state/evidence
directories use an exact protected private DACL. The SQLite file is separately re-secured with a
protected non-inheritable DACL, rejects multiple hard links, and applies the same file policy to
migration backups. User-selected report destinations do not automatically inherit this protection.
Report export rejects network/removable locations, reparse ancestors, symlink targets, and existing
files; it fsyncs a private temporary file before atomic publication.

Command evidence binds the executable SHA-256 and available file identity before and after the
query. Resource `evidence:<id>` links and adapter-run `evidence_ids` must resolve to evidence from
the same scan when written; a bounded integrity pass checks them again before any report byte is
emitted.

The `plan` command is deliberately non-executable. There is no `apply`, `clean-all`, global
`--yes`, direct filesystem delete, broker, or BleachBit `--clean` path in this milestone.

## Gate status

The [one-million-resource streaming benchmark](docs/evidence/benchmarks/2026-07-12-streaming-million.json)
and a [nine-adapter local smoke](docs/evidence/smoke/2026-07-11-local-adapters.json) are recorded,
and the repository now contains fail-closed protocols, schemas, templates, and validators for later
external verification. These are validation assets, not completed gate evidence:

| Gate | Available validation assets | Current status |
|---|---|---|
| G0 | [release-readiness protocol](docs/evidence/G0-release-readiness-protocol.md), source-boundary auditor, closed manifest validator, owner/CI/CodeQL templates, and release artifact validation | Open: project-owner approval and real revision-bound GitHub CI/CodeQL evidence are absent |
| G1 | [physical-boundary protocol](docs/evidence/G1-physical-boundary-protocol.md), incomplete manifest template, and physical/matrix validation contract | Open: the required real mount point, OneDrive, ReFS, removable, access-denied, and machine evidence is absent |
| G2 | [ProcMon/multi-machine protocol](docs/evidence/G2-procmon-smoke-protocol.md), conservative CSV validator, service-state collector, incomplete machine template, and 2-physical + 1-VM matrix validator | Open: no complete ProcMon capture or required external machine/product matrix exists |
| G5 | [direct-FS race protocol](docs/evidence/G5-direct-fs-race-protocol.md), incomplete manifest template, and prerequisite/race/review validation contract | Blocked: G2/G3/G4 are not passed, and no direct-FS executor or real race/review evidence exists |
| G6 | [future broker-install protocol](docs/evidence/gates/G6-broker-verification.md), install-manifest schema, and read-only installed-tree verifier | Blocked: no broker, installer, signing, IPC, or supported-Windows matrix exists; verifier output deliberately keeps `g6_gate_passed=false` |

The release build creates a wheel, validated CycloneDX runtime SBOM, and SHA-256 manifest. It also
writes `artifacts/release-validation.json` outside the release payload, binding the source revision,
artifact hashes, builder/validator/lockfile hashes, clean-runtime installation, reproducibility,
wheel RECORD checks, schemas, and inventory-only CLI surface. A local `result=PASS` in that file
only proves those mechanical checks for that build; G0 accepts it only as one revision-bound input
alongside real owner, GitHub CI, CodeQL, source-boundary, and dependency-audit evidence.
The wheel itself carries byte-verified `LICENSE` and `THIRD_PARTY_NOTICES.md` files with a PEP 639
license expression. The GPLv3 file is also checked against the canonical complete GNU text rather
than merely compared with the repository copy; this packaging fact still does not substitute for
the owner's G0 decision.

No G0, G1, G2, G5, or G6 gate is currently declared passed. See
[the implementation ledger](docs/implementation-status.md) and
[the evidence index](docs/evidence/README.md) for the exact remaining requirements. The
[v2 traceability matrix](docs/requirements-traceability.md) maps every initial work package and
Claude review item to its implementation evidence or explicit blocker.

## License

GPL-3.0-or-later. The license choice must be confirmed by the project owner before the first
public release. No BleachBit, Winapp2, or third-party cleaner rules are vendored.
