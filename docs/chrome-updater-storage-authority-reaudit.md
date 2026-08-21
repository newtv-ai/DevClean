# Chrome / Chromium Updater storage authority re-audit — 2026-08

## Scope

This pass re-checks the Google/Chromium Updater storage rules that can create raw deletion authority. It is intentionally narrow: the broader Chromium-browser family remains in the full second-pass queue until Chrome, Edge, Brave, Vivaldi and Opera are all re-verified on current sources.

## Current primary source

Chromium Updater Functional Specification (current `main`):

- https://chromium.googlesource.com/chromium/src/+/main/docs/updater/functional_spec.md

The current specification says downloads are cached in the updater install root's `crx_cache`, with at most one cached item per app ID. That remains a narrow updater-owned regenerable cache lane.

The same specification gives the updater logs their own lifecycle: all logs go to `updater.log`; once the log reaches 5 MiB the updater attempts on startup to rotate it to `updater.log.old`, replacing an existing old log. Rotation can be delayed while another updater is running. The updater also preserves a final log on uninstall, and `prefs.json` is persistent updater state.

## Finding

DevClean previously treated `updater.log` and `updater.log.old` as raw TOOL-delete files after a locally invented 7-day / 1 MiB threshold. The source does not define that retention rule. Instead it defines a vendor-owned size/rotation lifecycle and uses the logs as diagnostic state. Age and size therefore describe possible benefit, not deletion authority.

## Correction

- `crx_cache`: remains the existing source-identified updater cache lane; no widening.
- `updater.log`: KEEP / protected vendor-rotated diagnostic state.
- `updater.log.old`: KEEP / protected vendor-managed rotated diagnostic state.
- `prefs.json`, updater binaries/version state and legacy Google Update state: remain protected.
- AI/user learned rules cannot reintroduce deletion authority for the protected updater logs because application KEEP semantics remain a hard boundary.

## Revisit trigger

Only add a positive log-cleanup lane if Chromium/Google documents a bounded cleanup API/command or an explicit supported deletion lifecycle for these exact diagnostics. Do not infer one from age, size, process-idle state or the fact that rotation exists.
