from pathlib import Path

chrome = Path("src/devclean/core/chrome_cleanup.py")
text = chrome.read_text(encoding="utf-8")
old = '''    _rule(
        "chrome-updater-log",
        "updater.log",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "Chromium Updater diagnostic log",
        root_kind="updater",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),
    _rule(
        "chrome-updater-old-log",
        "updater.log.old",
        MatchKind.EXACT,
        DecisionOwner.TOOL,
        RebuildCost.NONE,
        "Rotated Chromium Updater diagnostic log",
        root_kind="updater",
        idle_days=7,
        min_reclaim_bytes=_MIB,
        requires_process_closed=True,
        size_sensitive_idle=False,
    ),'''
new = '''    _rule(
        "chrome-updater-log",
        "updater.log",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chromium Updater current diagnostic log (vendor-rotated)",
        root_kind="updater",
    ),
    _rule(
        "chrome-updater-old-log",
        "updater.log.old",
        MatchKind.EXACT,
        DecisionOwner.KEEP,
        RebuildCost.HIGH,
        "Chromium Updater rotated diagnostic log (vendor-managed)",
        root_kind="updater",
    ),'''
if old not in text:
    raise SystemExit("Chrome updater log rule block changed; refusing stale patch")
chrome.write_text(text.replace(old, new), encoding="utf-8")

test = Path("tests/test_chrome_cleanup.py")
t = test.read_text(encoding="utf-8")
old_test = '''    prefs = match_application_rule(base + r"\\prefs.json", _env())
    binary = match_application_rule(base + r"\\140.0.0.0\\updater.exe", _env())
    assert cache is not None
    assert cache.rule_id == "chrome-updater-crx-cache"
    assert cache.owner is DecisionOwner.TOOL
    assert prefs is not None and prefs.rule_id == "chrome-updater-state"
    assert prefs.owner is DecisionOwner.KEEP
    assert binary is not None and binary.rule_id == "chrome-updater-state"
    assert binary.owner is DecisionOwner.KEEP
'''
new_test = '''    current_log = match_application_rule(base + r"\\updater.log", _env())
    old_log = match_application_rule(base + r"\\updater.log.old", _env())
    prefs = match_application_rule(base + r"\\prefs.json", _env())
    binary = match_application_rule(base + r"\\140.0.0.0\\updater.exe", _env())
    assert cache is not None
    assert cache.rule_id == "chrome-updater-crx-cache"
    assert cache.owner is DecisionOwner.TOOL
    assert current_log is not None and current_log.rule_id == "chrome-updater-log"
    assert current_log.owner is DecisionOwner.KEEP
    assert old_log is not None and old_log.rule_id == "chrome-updater-old-log"
    assert old_log.owner is DecisionOwner.KEEP
    assert prefs is not None and prefs.rule_id == "chrome-updater-state"
    assert prefs.owner is DecisionOwner.KEEP
    assert binary is not None and binary.rule_id == "chrome-updater-state"
    assert binary.owner is DecisionOwner.KEEP
'''
if old_test not in t:
    raise SystemExit("Chrome updater test block changed; refusing stale patch")
test.write_text(t.replace(old_test, new_test), encoding="utf-8")

authority = Path("tests/test_chrome_rule_authority.py")
a = authority.read_text(encoding="utf-8")
needle = '        r"C:\\Users\\person\\AppData\\Local\\Google\\GoogleUpdater\\prefs.json",\n'
replacement = (
    '        r"C:\\Users\\person\\AppData\\Local\\Google\\GoogleUpdater\\updater.log",\n'
    '        r"C:\\Users\\person\\AppData\\Local\\Google\\GoogleUpdater\\updater.log.old",\n'
    + needle
)
if needle not in a:
    raise SystemExit("Chrome authority test anchor changed; refusing stale patch")
authority.write_text(a.replace(needle, replacement, 1), encoding="utf-8")

doc = Path("docs/chrome-updater-storage-authority-reaudit.md")
doc.write_text('''# Chrome / Chromium Updater storage authority re-audit — 2026-08

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
''', encoding="utf-8")

tracker = Path("docs/full-rule-reaudit-2026-08.md")
tr = tracker.read_text(encoding="utf-8")
old_row = "| Chromium browsers: Chrome / Edge / Brave / Vivaldi / Opera | ⏳ queued (Brave/Vivaldi/Opera recent authority corrections already landed) |"
new_row = "| Chromium browsers: Chrome / Edge / Brave / Vivaldi / Opera | ⏳ queued (Chrome updater-log correction plus Brave/Vivaldi/Opera authority corrections landed; full family re-verification still pending) |"
if old_row not in tr:
    raise SystemExit("Chromium tracker row changed; refusing stale patch")
tracker.write_text(tr.replace(old_row, new_row), encoding="utf-8")
