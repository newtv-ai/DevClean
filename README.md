# Reclaimer

Reclaimer is an evidence-first Windows disk scanner with an explicit, recoverable Recycle Bin
workflow for developer and AI workstations.

## Current status

**Pre-alpha.** It scans local files and can move a small, explicitly selected set of scanned
regular files to the Windows Recycle Bin. It does not permanently delete files, run maintenance,
request elevation, start vendor services, or invoke BleachBit cleaning.

The first supported execution platform will be Windows 11 x64 versions that are still serviced
by Microsoft at release time. Windows 10 is best-effort inventory only.

## Safety boundary

- A recycle request accepts only exact candidate IDs from a completed local scan; it never accepts
  a path, glob, directory, or `--yes` flag.
- Every selected file is rechecked immediately before the Shell call: fixed local volume, no
  reparse/Cloud Files boundary, 128-bit file identity, timestamps, size, attributes and single
  hard-link count must all match the scan snapshot.
- Recycle refuses directories, reparse points, Cloud Files placeholders, hard-linked files,
  Reclaimer state/evidence, `.git`, `.env*`, key/certificate files, and known editor-history paths.
- The only file mutation uses the Windows Recycle Bin; the Shell receives a full path with
  `FOF_ALLOWUNDO` and warning flags, never `DeleteFile` or a silent permanent-delete fallback.
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

# After reviewing the report, recycle only the exact scanned file IDs you explicitly select.
# The command prints the full paths and requires typing RECYCLE <scan-id> in an interactive terminal.
uv run reclaimer recycle <scan-id> --select <candidate-id>
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

The `plan` command remains deliberately non-executable. `recycle` is a separate, interactive
Recycle Bin path for exact file records only; there is no `apply`, `clean-all`, global `--yes`,
directory deletion, direct permanent delete, broker, or BleachBit `--clean` path.

## Gate status

The [one-million-resource streaming benchmark](docs/evidence/benchmarks/2026-07-12-streaming-million.json)
and a [nine-adapter local smoke](docs/evidence/smoke/2026-07-11-local-adapters.json) are recorded.
They are supporting engineering evidence, not prerequisites for local scan-and-recycle use.

| Gate | Available validation assets | Current status |
|---|---|---|
| Local recycle | scanner identity snapshot, protected-path deny list, interactive confirmation, Windows Recycle Bin bridge, and unit/integration smoke | Implemented for regular files; use is intentionally restricted to small, exact selections |
| Future vendor GC | existing adapter evidence and plan scaffolding | Not implemented |
| Direct permanent deletion / broker | future protocol assets only | Not implemented |

The release build creates a wheel, validated CycloneDX runtime SBOM, and SHA-256 manifest. It also
writes `artifacts/release-validation.json` outside the release payload, binding the source revision,
artifact hashes, builder/validator/lockfile hashes, clean-runtime installation, reproducibility,
wheel RECORD checks, schemas, and the safe local-recycle CLI surface. A local `result=PASS` proves
those mechanical checks for that build. The older G0 process adds optional public-release evidence,
not a requirement to use the local workflow.
The wheel itself carries byte-verified `LICENSE` and `THIRD_PARTY_NOTICES.md` files with a PEP 639
license expression. The GPLv3 file is also checked against the canonical complete GNU text rather
than merely compared with the repository copy; this packaging fact still does not substitute for
the owner's G0 decision.

The older G0/G1/G2 protocol assets remain available for an eventual public release or expanded
execution scope, but they are not needed to run this local, Recycle-Bin-only workflow.

## License

GPL-3.0-or-later. The license choice must be confirmed by the project owner before the first
public release. No BleachBit, Winapp2, or third-party cleaner rules are vendored.
