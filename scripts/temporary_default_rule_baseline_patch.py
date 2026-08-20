from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Packaged defaults are product policy, not a snapshot of one developer machine's
# learned decisions. Remove every persisted decision from the shipped templates;
# deterministic product knowledge belongs in audited classification/application
# rules, while user/AI decisions belong only in the user's DevClean-data sidecar.
for relative in (
    "src/devclean/config/delete-rules.json",
    "src/devclean/config/keep-rules.json",
):
    path = ROOT / relative
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload.get("rules"), list) or not payload["rules"]:
        raise RuntimeError(f"expected legacy learned rules in {relative}")
    payload["rules"] = []
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

impl_path = ROOT / "src/devclean/core/_user_rules_impl.py"
text = impl_path.read_text(encoding="utf-8")
old = '''def load_rules(*, create_missing: bool = True) -> UserRules:
    """Load the three current-format documents.

    Missing files are copied from the packaged templates once.  Complete
    activity files are always read from ``DevClean-data`` beside the program.
    If only the visible default backup was deleted, rebuilding it from the
    packaged templates is best-effort and can never block activity-rule loading.
    """

    paths = (scan_rules_path(), delete_rules_path(), keep_rules_path())
    if create_missing:
        rules_dir().mkdir(parents=True, exist_ok=True)
        missing = tuple(path for path in paths if not path.is_file())
        backup_source: tuple[str, str, str] | None = None
        if missing:
            defaults = (
                _default_backup_documents()
                if default_backup_path().is_file()
                else _packaged_documents()
            )
            backup_source = defaults
            for path, text in zip(paths, defaults, strict=True):
                if not path.is_file():
                    _atomic_write(path, text)
        if not default_backup_path().is_file():
            with suppress(
                ImportError, OSError, RuleConfigError, UnicodeError
            ):
                _ensure_default_backup(
                    backup_source or _packaged_documents()
                )
            # The three activity files remain authoritative and usable. Restore
            # stays unavailable only when the packaged copy itself cannot be
            # read or the sidecar directory cannot be written.
    return parse_rule_documents(*read_rule_documents())


def restore_default_rules() -> UserRules:
    """Restore all three active files from the visible sidecar backup."""

    defaults = _default_backup_documents()
    restored = parse_rule_documents(*defaults)
    rules_dir().mkdir(parents=True, exist_ok=True)
    for path, text in zip(
        (scan_rules_path(), delete_rules_path(), keep_rules_path()),
        defaults,
        strict=True,
    ):
        _atomic_write(path, text)
    return restored
'''
new = '''def load_rules(*, create_missing: bool = True) -> UserRules:
    """Load the three current-format documents.

    Active sidecar files stay authoritative. Packaged templates are consulted
    best-effort on normal launches so the visible default backup tracks the
    *current executable* rather than whichever DevClean version first created
    the data directory. Missing activity files prefer the current packaged
    template and fall back to the sidecar backup only when packaged resources
    are temporarily unavailable.

    One historical release accidentally shipped machine-specific learned
    decisions inside the packaged DELETE/KEEP templates. When an old sidecar
    backup proves that an unchanged active decision came from that packaged
    baseline, remove only that exact inherited entry. User/AI rules created
    later, or edited since installation, are not present byte-for-byte in the
    old backup and are preserved.
    """

    paths = (scan_rules_path(), delete_rules_path(), keep_rules_path())
    if not create_missing:
        return parse_rule_documents(*read_rule_documents())

    rules_dir().mkdir(parents=True, exist_ok=True)
    packaged: tuple[str, str, str] | None = None
    with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
        packaged = _packaged_documents()

    missing = tuple(path for path in paths if not path.is_file())
    if missing:
        defaults: tuple[str, str, str] | None = packaged
        if defaults is None and default_backup_path().is_file():
            defaults = _default_backup_documents()
        if defaults is None:
            # Preserve the previous clear failure when neither current packaged
            # resources nor a valid sidecar default backup can supply a missing
            # required document.
            defaults = _packaged_documents()
        for path, document in zip(paths, defaults, strict=True):
            if not path.is_file():
                _atomic_write(path, document)

    if packaged is not None and default_backup_path().is_file():
        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
            _migrate_legacy_packaged_decisions(packaged)

    if packaged is not None:
        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
            _ensure_default_backup(packaged)

    return parse_rule_documents(*read_rule_documents())


def restore_default_rules() -> UserRules:
    """Restore active files from the current executable's packaged defaults.

    The visible sidecar ZIP remains a fallback for a damaged/unavailable
    resource package, but an old ZIP must never pin "restore defaults" to a
    previous DevClean release.
    """

    try:
        defaults = _packaged_documents()
    except (ImportError, OSError, RuleConfigError, UnicodeError):
        defaults = _default_backup_documents()
    restored = parse_rule_documents(*defaults)
    rules_dir().mkdir(parents=True, exist_ok=True)
    for path, document in zip(
        (scan_rules_path(), delete_rules_path(), keep_rules_path()),
        defaults,
        strict=True,
    ):
        _atomic_write(path, document)
    with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
        _ensure_default_backup(defaults)
    return restored


def _migrate_legacy_packaged_decisions(
    packaged: tuple[str, str, str],
) -> None:
    """Remove only unchanged decision entries proven to come from old defaults."""

    current_defaults = parse_rule_documents(*packaged)
    if current_defaults.delete.rules or current_defaults.keep.rules:
        # This migration is deliberately tied to the neutral-default contract.
        # If a future release intentionally ships decision entries, do not infer
        # that older sidecar entries are contamination.
        return

    legacy_defaults = parse_rule_documents(*_default_backup_documents())
    legacy_delete = frozenset(legacy_defaults.delete.rules)
    legacy_keep = frozenset(legacy_defaults.keep.rules)
    if not legacy_delete and not legacy_keep:
        return

    active = parse_rule_documents(*read_rule_documents())
    delete_rules = tuple(rule for rule in active.delete.rules if rule not in legacy_delete)
    keep_rules = tuple(rule for rule in active.keep.rules if rule not in legacy_keep)
    if delete_rules == active.delete.rules and keep_rules == active.keep.rules:
        return

    save_rules(
        UserRules(
            scan=active.scan,
            delete=replace(active.delete, rules=delete_rules),
            keep=replace(active.keep, rules=keep_rules),
        )
    )
'''
if old not in text:
    raise RuntimeError("load/restore rules block did not match expected main")
text = text.replace(old, new)
impl_path.write_text(text, encoding="utf-8", newline="\n")

test_path = ROOT / "tests/_test_rules_impl.py"
test = test_path.read_text(encoding="utf-8")
old_assert = '''    assert rules.scan.delete_root_ids == set()\n    assert len(rules.scan.known_cleanup_roots) >= 30\n    assert MAX_DECISION_RULES == 100_000\n    assert parse_rule_documents(*render_rule_documents(rules)) == rules\n'''
new_assert = '''    assert rules.scan.delete_root_ids == set()\n    assert len(rules.scan.known_cleanup_roots) >= 30\n    assert rules.delete.rules == ()\n    assert rules.keep.rules == ()\n    assert rules.ai_rule_count == 0\n    assert MAX_DECISION_RULES == 100_000\n    assert parse_rule_documents(*render_rule_documents(rules)) == rules\n'''
if old_assert not in test:
    raise RuntimeError("packaged-rules test block did not match expected main")
test = test.replace(old_assert, new_assert)

anchor = '''def test_ai_rules_port_user_profile_and_reuse_dated_path_shapes(\n'''
new_test = r'''def test_legacy_packaged_decisions_are_removed_without_erasing_new_user_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    clean_defaults = default_rules()
    clean_documents = render_rule_documents(clean_defaults)
    legacy_delete = DecisionRule(
        rule_id="legacy-packaged-delete",
        group="ai_import",
        match=RuleMatch.EXACT_PATH,
        value=r"C:\Users\legacy\machine-specific-cache.bin",
        source="AI_IMPORT",
        reason="accidentally shipped machine decision",
        updated_at="2026-07-27T00:00:00+00:00",
    )
    user_delete = DecisionRule(
        rule_id="real-user-delete",
        group="user_decision",
        match=RuleMatch.EXACT_PATH,
        value=r"D:\user-selected\disposable.bin",
        source="USER_DECISION",
        reason="created after installation",
        updated_at="2026-08-20T00:00:00+00:00",
    )
    legacy = UserRules(
        scan=clean_defaults.scan,
        delete=replace(clean_defaults.delete, rules=(legacy_delete,)),
        keep=clean_defaults.keep,
    )
    legacy_documents = render_rule_documents(legacy)

    from devclean.core import user_rules as module

    module.rules_dir().mkdir(parents=True, exist_ok=True)
    for path, document in zip(
        (module.scan_rules_path(), module.delete_rules_path(), module.keep_rules_path()),
        render_rule_documents(
            UserRules(
                scan=legacy.scan,
                delete=replace(legacy.delete, rules=(legacy_delete, user_delete)),
                keep=legacy.keep,
            )
        ),
        strict=True,
    ):
        path.write_text(document, encoding="utf-8")
    with zipfile.ZipFile(module.default_backup_path(), "w") as archive:
        for name, document in zip(
            (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME),
            legacy_documents,
            strict=True,
        ):
            archive.writestr(name, document.encode("utf-8"))

    monkeypatch.setattr(module, "_packaged_documents", lambda: clean_documents)
    migrated = load_rules()

    assert legacy_delete not in migrated.delete.rules
    assert user_delete in migrated.delete.rules
    assert migrated.decision_for(user_delete.value) is RuleDecision.DELETE
    with zipfile.ZipFile(default_backup_path()) as archive:
        assert archive.read(DELETE_RULES_NAME).decode("utf-8") == clean_documents[1]


def test_restore_defaults_prefers_current_packaged_templates_over_stale_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    clean = default_rules()
    clean_documents = render_rule_documents(clean)
    stale_rule = DecisionRule(
        rule_id="stale-default-delete",
        group="ai_import",
        match=RuleMatch.EXACT_PATH,
        value=r"C:\stale-default.bin",
        source="AI_IMPORT",
    )
    stale = UserRules(
        scan=clean.scan,
        delete=replace(clean.delete, rules=(stale_rule,)),
        keep=clean.keep,
    )
    stale_documents = render_rule_documents(stale)

    from devclean.core import user_rules as module

    module.rules_dir().mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(module.default_backup_path(), "w") as archive:
        for name, document in zip(
            (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME),
            stale_documents,
            strict=True,
        ):
            archive.writestr(name, document.encode("utf-8"))
    monkeypatch.setattr(module, "_packaged_documents", lambda: clean_documents)

    restored = restore_default_rules()

    assert restored.delete.rules == ()
    assert restored.keep.rules == ()
    assert restored.ai_rule_count == 0
    with zipfile.ZipFile(default_backup_path()) as archive:
        assert archive.read(DELETE_RULES_NAME).decode("utf-8") == clean_documents[1]


'''
if anchor not in test:
    raise RuntimeError("test insertion anchor not found")
test = test.replace(anchor, new_test + anchor, 1)
test_path.write_text(test, encoding="utf-8", newline="\n")

# Durable audit note for the broader re-audit requested by the product owner.
audit_path = ROOT / "docs/default-rule-baseline-reaudit.md"
audit_path.write_text(
    """# Default rule baseline re-audit\n\n"
    "Audited: 2026-08-20\n\n"
    "## Finding\n\n"
    "The packaged `delete-rules.json` and `keep-rules.json` were not neutral product defaults. "
    "They contained machine-specific `AI_IMPORT` decisions learned during development. The build "
    "script embeds those exact JSON files into `DevClean.exe`, and `load_rules()` copies packaged "
    "templates into a new user's `DevClean-data` when the sidecar files are missing. Those learned "
    "decisions therefore had the ability to become product-wide defaults on unrelated machines.\n\n"
    "Examples found during the re-audit included direct DELETE decisions for browser/model assets, "
    "package-cache internals, editor state databases, embedded OCR model weights, debug symbols, "
    "runtime/JDK artifacts and application resources merely because they were thought redownloadable "
    "or regenerable on the development machine. That is incompatible with DevClean's source-first "
    "authority model. A learned judgment is evidence about one observation, not universal delete "
    "authority.\n\n"
    "## Correction\n\n"
    "- Packaged DELETE and KEEP decision arrays are now empty.\n"
    "- Audited deterministic semantics remain in product classification/application rules instead.\n"
    "- User/AI decisions continue to live only in the user's sidecar after that user actually makes/imports them.\n"
    "- Existing installations are migrated conservatively: if the old default-backup ZIP proves an "
    "  active decision was shipped by the old baseline and the entry is still exactly unchanged, it "
    "  is removed. Later user/AI decisions and edited entries are preserved.\n"
    "- The visible default-backup ZIP is refreshed best-effort from the current executable, so "
    "  `restore defaults` no longer pins a user to defaults from the first installed version.\n"
    "- Missing activity files prefer current packaged templates and use the sidecar ZIP only as an "
    "  availability fallback.\n\n"
    "## Product rule\n\n"
    "No future release may ship `AI_IMPORT` or `USER_DECISION` entries in packaged defaults. If a "
    "decision is reliable for every user, encode it as a source-audited deterministic application or "
    "vendor lifecycle rule with tests. If it is not reliable for every user, it cannot be a product "
    "default.\n\n"
    "This is phase 1 of the 2026-08 full rule re-audit. The next phases re-check scan-root scope, "
    "generic classification heuristics and then each application-specific rule to reduce both "
    "USER_REVIEW and AI_REVIEW without widening destructive authority.\n"
    """,
    encoding="utf-8",
    newline="\n",
)
