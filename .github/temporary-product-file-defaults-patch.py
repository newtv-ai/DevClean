from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one replacement in {path}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


impl = ROOT / "src/devclean/core/_user_rules_impl.py"
replace_once(
    impl,
    '_DIRECTORY_DECISION_SOURCE: Final = "USER_DIRECTORY_DECISION"\n',
    '_DIRECTORY_DECISION_SOURCE: Final = "USER_DIRECTORY_DECISION"\n'
    '_PRODUCT_RULE_SOURCE: Final = "PRODUCT_AUDITED"\n',
)
replace_once(
    impl,
    '''    if packaged is not None:\n        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):\n            _ensure_default_backup(packaged)\n\n    return parse_rule_documents(*read_rule_documents())\n''',
    '''    if packaged is not None:\n        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):\n            _ensure_default_backup(packaged)\n\n    active = parse_rule_documents(*read_rule_documents())\n    if packaged is None:\n        return active\n    current_defaults = parse_rule_documents(*packaged)\n    return _overlay_packaged_product_rules(active, current_defaults)\n''',
)
old_migration = '''def _migrate_legacy_packaged_decisions(\n    packaged: tuple[str, str, str],\n) -> None:\n    """Remove only unchanged decision entries proven to come from old defaults."""\n\n    current_defaults = parse_rule_documents(*packaged)\n    if current_defaults.delete.rules or current_defaults.keep.rules:\n        # This migration is deliberately tied to the neutral-default contract.\n        # If a future release intentionally ships decision entries, do not infer\n        # that older sidecar entries are contamination.\n        return\n\n    legacy_defaults = parse_rule_documents(*_default_backup_documents())\n    legacy_delete = frozenset(legacy_defaults.delete.rules)\n    legacy_keep = frozenset(legacy_defaults.keep.rules)\n    if not legacy_delete and not legacy_keep:\n        return\n\n    active = parse_rule_documents(*read_rule_documents())\n    delete_rules = tuple(rule for rule in active.delete.rules if rule not in legacy_delete)\n    keep_rules = tuple(rule for rule in active.keep.rules if rule not in legacy_keep)\n    if delete_rules == active.delete.rules and keep_rules == active.keep.rules:\n        return\n\n    save_rules(\n        UserRules(\n            scan=active.scan,\n            delete=replace(active.delete, rules=delete_rules),\n            keep=replace(active.keep, rules=keep_rules),\n        )\n    )\n\n\n'''
new_migration = '''def _migrate_legacy_packaged_decisions(\n    packaged: tuple[str, str, str],\n) -> None:\n    """Remove unchanged accidental AI imports while allowing audited defaults."""\n\n    # The historical contamination was specifically a snapshot of AI_IMPORT\n    # decisions from one development machine. Product-audited rules deliberately\n    # shipped by a newer executable must not disable this cleanup migration.\n    parse_rule_documents(*packaged)\n    legacy_defaults = parse_rule_documents(*_default_backup_documents())\n    legacy_delete = frozenset(\n        rule for rule in legacy_defaults.delete.rules if rule.source == "AI_IMPORT"\n    )\n    legacy_keep = frozenset(\n        rule for rule in legacy_defaults.keep.rules if rule.source == "AI_IMPORT"\n    )\n    if not legacy_delete and not legacy_keep:\n        return\n\n    active = parse_rule_documents(*read_rule_documents())\n    delete_rules = tuple(rule for rule in active.delete.rules if rule not in legacy_delete)\n    keep_rules = tuple(rule for rule in active.keep.rules if rule not in legacy_keep)\n    if delete_rules == active.delete.rules and keep_rules == active.keep.rules:\n        return\n\n    save_rules(\n        UserRules(\n            scan=active.scan,\n            delete=replace(active.delete, rules=delete_rules),\n            keep=replace(active.keep, rules=keep_rules),\n        )\n    )\n\n\ndef _overlay_packaged_product_rules(active: UserRules, packaged: UserRules) -> UserRules:\n    """Apply current product file knowledge independently of local history."""\n\n    product_delete = tuple(\n        rule for rule in packaged.delete.rules if rule.source == _PRODUCT_RULE_SOURCE\n    )\n    product_keep = tuple(\n        rule for rule in packaged.keep.rules if rule.source == _PRODUCT_RULE_SOURCE\n    )\n    local_delete = tuple(\n        rule for rule in active.delete.rules if rule.source != _PRODUCT_RULE_SOURCE\n    )\n    local_keep = tuple(rule for rule in active.keep.rules if rule.source != _PRODUCT_RULE_SOURCE)\n    return UserRules(\n        scan=active.scan,\n        delete=replace(active.delete, rules=(*product_delete, *local_delete)),\n        keep=replace(active.keep, rules=(*product_keep, *local_keep)),\n    )\n\n\n'''
replace_once(impl, old_migration, new_migration)

# Curate only common file-level knowledge whose semantics survived source review.
delete_path = ROOT / "src/devclean/config/delete-rules.json"
delete_doc = json.loads(delete_path.read_text(encoding="utf-8"))
delete_doc["rules"] = [
    {
        "id": "product_nvidia_dxcache_nvph",
        "group": "product_audited",
        "enabled": True,
        "match": "path_glob",
        "value": r"%LOCALAPPDATA%\NVIDIA\DXCache\*.nvph",
        "source": "PRODUCT_AUDITED",
        "reason": "NVIDIA 驱动着色器磁盘缓存；删除只会丢失编译加速，驱动会重新生成。",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
    {
        "id": "product_nvidia_dxcache_bin",
        "group": "product_audited",
        "enabled": True,
        "match": "path_glob",
        "value": r"%LOCALAPPDATA%\NVIDIA\DXCache\*.bin",
        "source": "PRODUCT_AUDITED",
        "reason": "NVIDIA 驱动着色器磁盘缓存；删除后可能短暂重新编译，但不会删除应用数据。",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
    {
        "id": "product_nvidia_glcache_bin",
        "group": "product_audited",
        "enabled": True,
        "match": "path_glob",
        "value": r"%LOCALAPPDATA%\NVIDIA\GLCache\*\*\*.bin",
        "source": "PRODUCT_AUDITED",
        "reason": "NVIDIA OpenGL 着色器缓存文件；驱动版本变化时本就会重新编译/重建。",
        "updated_at": "2026-08-21T00:00:00+00:00",
    },
]
delete_path.write_text(json.dumps(delete_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

keep_path = ROOT / "src/devclean/config/keep-rules.json"
keep_doc = json.loads(keep_path.read_text(encoding="utf-8"))
keep_doc["rules"] = [
    {
        "id": "product_user_jdk_runtime_modules",
        "group": "product_audited",
        "enabled": True,
        "match": "path_glob",
        "value": r"%USERPROFILE%\.jdks\*\lib\modules",
        "source": "PRODUCT_AUDITED",
        "reason": "JDK lib 是运行时内部实现；Oracle 明确要求不要修改，不能把 lib/modules 当缓存清理。",
        "updated_at": "2026-08-21T00:00:00+00:00",
    }
]
keep_path.write_text(json.dumps(keep_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Update rule-engine expectations and prove current product rules overlay old sidecars.
tests = ROOT / "tests/_test_rules_impl.py"
replace_once(
    tests,
    '''    assert rules.delete.rules == ()\n    assert rules.keep.rules == ()\n    assert rules.ai_rule_count == 0\n''',
    '''    assert {rule.rule_id for rule in rules.delete.rules} == {\n        "product_nvidia_dxcache_nvph",\n        "product_nvidia_dxcache_bin",\n        "product_nvidia_glcache_bin",\n    }\n    assert {rule.rule_id for rule in rules.keep.rules} == {\n        "product_user_jdk_runtime_modules"\n    }\n    assert all(\n        rule.source == "PRODUCT_AUDITED"\n        for rule in (*rules.delete.rules, *rules.keep.rules)\n    )\n    assert rules.ai_rule_count == 0\n''',
)
replace_once(
    tests,
    '''    assert restored.delete.rules == ()\n    assert restored.keep.rules == ()\n    assert restored.ai_rule_count == 0\n''',
    '''    assert restored.delete.rules == clean.delete.rules\n    assert restored.keep.rules == clean.keep.rules\n    assert restored.ai_rule_count == 0\n''',
)
insert_after = '''def test_legacy_packaged_decisions_are_removed_without_erasing_new_user_rules(\n'''
new_test = '''def test_current_product_rules_overlay_existing_neutral_sidecars(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))\n    current = default_rules()\n    current_documents = render_rule_documents(current)\n    local_delete = DecisionRule(\n        rule_id="local-user-delete",\n        group="user_decision",\n        match=RuleMatch.EXACT_PATH,\n        value=r"D:\\user-selected\\disposable.bin",\n        source="USER_DECISION",\n        reason="local history must survive product updates",\n    )\n    neutral = UserRules(\n        scan=current.scan,\n        delete=replace(current.delete, rules=(local_delete,)),\n        keep=replace(current.keep, rules=()),\n    )\n\n    from devclean.core import user_rules as module\n\n    module.rules_dir().mkdir(parents=True, exist_ok=True)\n    for path, document in zip(\n        (module.scan_rules_path(), module.delete_rules_path(), module.keep_rules_path()),\n        render_rule_documents(neutral),\n        strict=True,\n    ):\n        path.write_text(document, encoding="utf-8")\n    monkeypatch.setattr(module, "_packaged_documents", lambda: current_documents)\n\n    loaded = load_rules()\n\n    assert local_delete in loaded.delete.rules\n    assert loaded.decision_for(local_delete.value) is RuleDecision.DELETE\n    assert {\n        rule.rule_id for rule in loaded.delete.rules if rule.source == "PRODUCT_AUDITED"\n    } == {\n        "product_nvidia_dxcache_nvph",\n        "product_nvidia_dxcache_bin",\n        "product_nvidia_glcache_bin",\n    }\n    assert {\n        rule.rule_id for rule in loaded.keep.rules if rule.source == "PRODUCT_AUDITED"\n    } == {"product_user_jdk_runtime_modules"}\n\n\n'''
text = tests.read_text(encoding="utf-8")
if insert_after not in text:
    raise RuntimeError("could not locate migration test insertion point")
tests.write_text(text.replace(insert_after, new_test + insert_after, 1), encoding="utf-8")

boundary = ROOT / "tests/test_learned_rule_target_boundary.py"
text = boundary.read_text(encoding="utf-8")
text += '''\n\ndef test_product_file_rule_never_authorizes_same_named_directory(\n    tmp_path: Path, monkeypatch: pytest.MonkeyPatch\n) -> None:\n    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "data"))\n    monkeypatch.setenv("LOCALAPPDATA", r"C:\\Users\\person\\AppData\\Local")\n    path = r"C:\\Users\\person\\AppData\\Local\\NVIDIA\\DXCache\\sample.bin"\n    rules = default_rules()\n\n    assert rules.decision_for(path) is RuleDecision.DELETE\n    deletable, unsure = _partition(_item(path, directory=True), rules)\n\n    assert deletable == ()\n    assert [item.path for item in unsure] == [path]\n    assert rules.directory_decision_for(path) is None\n'''
boundary.write_text(text, encoding="utf-8")

tracker = ROOT / "docs/full-rule-reaudit-2026-08.md"
text = tracker.read_text(encoding="utf-8")
text = text.replace(
    "| Packaged DELETE/KEEP defaults | ⚠ #142 interim | backup/migration bug fixed; defaults temporarily neutralized, safe file-level knowledge will be selectively restored |",
    "| Packaged DELETE/KEEP defaults | ✅ phase 6 | raw machine history stays local; current audited product FILE rules overlay sidecars as a separate source |",
)
text = text.replace(
    "| Scan exclusions/pruning | ⏳ queued | verify no important audited cache is accidentally skipped and no user data is widened |",
    "| Scan exclusions/pruning | ✅ phase 5 | explicit/audited nested roots survive skipped-name ancestors; exclusions and descendant pruning still win |",
)
text = text.replace(
    "| Learned-rule portability/default restoration | ⏳ queued | re-audit old packaged file rules, generated glob/regex reuse, then selectively restore safe common-file knowledge |",
    "| Learned-rule portability/default restoration | ✅ phase 6 | restored only source-supported common FILE knowledge; rejected personal/history/install-state and already-covered app-cache guesses |",
)
tracker.write_text(text, encoding="utf-8")

(ROOT / "docs/product-file-defaults-reaudit.md").write_text('''# Product file defaults / learned-rule portability re-audit\n\nAudited: 2026-08-21\n\n## Architecture\n\nRaw `AI_IMPORT` and `USER_DECISION` history remains local. Reusable product knowledge uses a separate `PRODUCT_AUDITED` source. On every normal load, the current executable overlays only its product rules onto the active local sidecar rules; stale product copies in sidecars cannot pin an old product definition. `AI_IMPORT` still counts and clears separately.\n\nAll non-directory decision sources are consumed only through the file matcher. Directories use the separate `USER_DIRECTORY_DECISION` exact-path matcher, so a product glob such as an NVIDIA `*.bin` rule cannot authorize a directory whose name happens to end in `.bin`. Explicit kept directories continue to suppress descendant file deletes.\n\nThe legacy contamination migration now keys on the historical `AI_IMPORT` provenance instead of assuming packaged defaults are forever empty. This allows deliberately audited defaults without preserving the accidental development-machine snapshot that #142 removed.\n\n## Promoted DELETE knowledge\n\nThe old development-machine rules repeatedly observed files under `%LOCALAPPDATA%\\NVIDIA\\DXCache` and `%LOCALAPPDATA%\\NVIDIA\\GLCache`. NVIDIA documents shader disk caches as compiled-shader acceleration, says driver changes cause recompilation, documents automatic cache eviction, and explicitly describes removing an old GLCache as safe because the driver repopulates a fresh cache. The product therefore promotes only the observed Windows file shapes (`DXCache\\*.nvph`, `DXCache\\*.bin`, `GLCache\\*\\*\\*.bin`). It does **not** grant whole-directory authority.\n\nPrimary vendor references:\n\n- https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-gb/mergedProjects/nv3dENG/Manage_3D_Settings_%28reference%29.htm\n- https://download.nvidia.com/XFree86/Linux-x86_64/555.58/README/openglenvvariables.html\n\n## Promoted KEEP knowledge\n\nThe old rules also correctly identified JetBrains-managed JDK `lib\\modules` files under `%USERPROFILE%\\.jdks`. Oracle documents the installed JDK `lib` tree as private runtime implementation details that must not be modified. The product converts that observation into a conservative file-only KEEP glob. This is protection, not a cleanup candidate.\n\nPrimary vendor reference:\n\n- https://docs.oracle.com/en/java/javase/26/install/installed-directory-structure-jdk.html\n\n## Deliberately not restored\n\nThe rest of the old machine snapshot is not copied back merely because an AI once labeled it. Major rejected classes include:\n\n- Claude/Codex transcripts and current-version state: personal retention or current-use semantics;\n- JDK source archives, PDBs, GraalVM profiles and other installed payload: installation/feature state, not cache authority;\n- EasyOCR/model weights and browser local models: redownloadable does not mean low-cost or user-independent;\n- Android AVD snapshots: exact emulator lifecycle and user boot-performance preference belong to the Android-specific lane;\n- browser component/Safe Browsing caches, VSIX caches, npm cache and IDE indexes already covered by stronger application/vendor rules: duplicating them as generic learned globs would weaken the source-owned boundary;\n- Tencent/QQ dynamic packages and other third-party application resources: no current primary-source lifecycle proof was established in this pass;\n- MySQL/InnoDB files, SQLite WAL and similar persistent state: protection belongs to semantic hard guards/application state, not copied machine paths.\n\nThe promotion rule is therefore: a development-machine observation may seed research, but it becomes product knowledge only when the object type is common, file-scoped, source-supported, and no stronger source-specific rule already owns it.\n''', encoding="utf-8")
