from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# Individual files never inherit authority from a whole-tree vendor root.  The
# application classifier already ran first; if it did not match the file, the
# exact file semantics are unproven and must remain protected.  Attached TOOL
# provenance is used only for exact whole-directory capabilities, which are
# freshly revalidated before mutation.
replace_once(
    "src/devclean/core/triage.py",
    '''        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n            if _has_audited_vendor_authority(known):\n                return _Classification(\n                    known.category,\n                    ReviewLane.DETERMINISTIC_CANDIDATE,\n                    RiskTier.LOW,\n                    EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                    Actionability.REVIEW_PLAN,\n                    ExecutionPolicy.USER_CHOICE_DELETE,\n                    RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,\n                    f"{known.label}：携带精确的应用 TOOL 生命周期规则，工具确定可清理",\n                    ("known_root", "vendor_managed", "audited_application_rule", "tool_decision"),\n                )\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.FILESYSTEM_OBSERVATION,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.NONE,\n                f"{known.label}：旧/静态配置仅声明厂商目录，没有附带可验证的应用删除契约；工具直接保护",\n                ("known_root", "vendor_managed", "missing_application_rule", "report_only"),\n            )\n''',
    '''        if known.policy is CleanupPolicy.VENDOR_MANAGED:\n            provenance = (\n                "audited_whole_tree_root"\n                if _has_audited_vendor_authority(known)\n                else "missing_application_rule"\n            )\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.FILESYSTEM_OBSERVATION,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.NONE,\n                (\n                    f"{known.label}：目录级厂商规则不能替代具体文件语义；"\n                    "应用分类器未确认此文件，工具直接保护"\n                ),\n                ("known_root", "vendor_managed", provenance, "unmatched_file", "report_only"),\n            )\n''',
)

# Static Android system-images was only a legacy inventory label.  The richer
# application layer already classifies installed system images KEEP, and the
# packaged discovery anchor now correctly says REPORT_ONLY.
replace_once(
    "tests/test_android_sdk_cleanup.py",
    '''    # The legacy scan catalog inventories system-images as vendor-managed storage,\n    # but the application semantic layer above it is authoritative and KEEP. This\n    # regression prevents a large installed emulator image from becoming generic\n    # cleanup merely because it is old or large.\n    assert image_item.category is CleanupCategory.ANDROID_SDK_PAYLOAD\n    assert image_item.policy is CleanupPolicy.VENDOR_MANAGED\n''',
    '''    # The static scan catalog is discovery-only. Installed system images are\n    # protected here, while the application semantic layer remains authoritative\n    # KEEP for their actual payload files.\n    assert image_item.category is CleanupCategory.ANDROID_SDK_PAYLOAD\n    assert image_item.policy is CleanupPolicy.REPORT_ONLY\n''',
)

# An old VENDOR_MANAGED directory with no application rule must now fail at the
# execution boundary.  Windows.old cleanup is handled by its dedicated Windows
# maintenance lane, not a generic whole-tree vendor label.
cleanup_test = Path("tests/test_cleanup_execution.py")
cleanup_text = cleanup_test.read_text(encoding="utf-8")
marker = "def test_windows_old_root_itself_reaches_directory_purger(\n"
pos = cleanup_text.find(marker)
if pos < 0:
    raise RuntimeError("Windows.old execution regression anchor missing")
cleanup_text = cleanup_text[:pos] + r'''def test_legacy_windows_old_vendor_root_cannot_reach_directory_purger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import devclean.core.postscan_cleanup as cleanup

    target = Path(r"G:\Windows.old")
    boundary_root = Path("G:\\")
    known = KnownCleanupRoot(
        path=target,
        category=CleanupCategory.WINDOWS_UPDATE,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Windows 旧系统目录",
        allow_inside_system_anchor=True,
        delete_root_itself=True,
    )
    target_metadata = _metadata(target, directory=True)
    root_metadata = _metadata(boundary_root, directory=True)
    record = ScanRecord(
        root=str(target),
        path=str(target),
        kind=ScanRecordKind.DIRECTORY,
        depth=0,
        volume_serial=target_metadata.volume_serial,
        file_id=target_metadata.file_id,
        file_id_kind=target_metadata.file_id_kind,
        link_count=target_metadata.link_count,
        attributes=target_metadata.attributes,
        creation_time_ns=target_metadata.creation_time_ns,
        last_write_time_ns=target_metadata.last_write_time_ns,
    )
    item = TriageItem(
        record=record,
        path=str(target),
        logical_size=0,
        allocated_size=None,
        category=CleanupCategory.WINDOWS_UPDATE,
        source_domain=SourceDomain.WINDOWS_SYSTEM,
        lane=ReviewLane.DETERMINISTIC_CANDIDATE,
        risk_tier=RiskTier.LOW,
        evidence_kind=EvidenceKind.KNOWN_ROOT_HEURISTIC,
        actionability=Actionability.REVIEW_PLAN,
        execution_policy=ExecutionPolicy.USER_CHOICE_DELETE,
        recovery=RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT,
        reason="legacy forged vendor root",
        tags=("whole_directory", "known_cache_root"),
        target_kind=CleanupTargetKind.DIRECTORY,
        directory_scope=DirectoryScope.KNOWN_CACHE_ROOT,
    )

    def read(path: Path) -> FileSystemMetadata:
        return root_metadata if path == boundary_root else target_metadata

    monkeypatch.setattr(cleanup, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(cleanup, "read_file_metadata", read)
    rules = default_rules()
    with pytest.raises(CleanupRefusal, match="attached audited application TOOL rule"):
        candidate_from_directory_item(
            item,
            DirectorySubtreeTotals(files=2, logical_bytes=20, allocated_bytes=20),
            known_roots=(known,),
            delete_config=rules.delete.classification,
            keep_config=rules.keep.classification,
        )
'''
cleanup_test.write_text(cleanup_text, encoding="utf-8", newline="\n")

replace_once(
    "tests/test_whole_tree_policy.py",
    '''def test_configured_non_application_root_keeps_existing_directory_semantics(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    import devclean.core.whole_tree_policy as policy\n\n    root = tmp_path / "Windows.old"\n\n    def must_not_scan(*_args: object, **_kwargs: object) -> object:\n        raise AssertionError("configured roots must not enter application policy scan")\n\n    monkeypatch.setattr(policy, "scan_roots", must_not_scan)\n\n    assert require_application_whole_tree_policy(root, (_known(root, None),)) is None\n''',
    '''def test_configured_vendor_root_without_application_rule_fails_closed(\n    tmp_path: Path,\n    monkeypatch: pytest.MonkeyPatch,\n) -> None:\n    import devclean.core.whole_tree_policy as policy\n\n    root = tmp_path / "Windows.old"\n\n    def must_not_scan(*_args: object, **_kwargs: object) -> object:\n        raise AssertionError("static vendor roots must fail before application policy scan")\n\n    monkeypatch.setattr(policy, "scan_roots", must_not_scan)\n\n    with pytest.raises(WholeTreePolicyRefusal, match="static vendor-managed"):\n        require_application_whole_tree_policy(root, (_known(root, None),))\n''',
)

# Fix strict typing in the new regression suite and add a file-level positive
# provenance / negative authority test.
replace_once(
    "tests/test_static_vendor_root_fallback.py",
    '''    RiskTier,\n    triage_directory,\n''',
    '''    RiskTier,\n    TriageItem,\n    triage_directory,\n''',
)
replace_once(
    "tests/test_static_vendor_root_fallback.py",
    "def _assert_protected(item) -> None:\n    assert item is not None\n",
    "def _assert_protected(item: TriageItem | None) -> None:\n    assert item is not None\n",
)
replace_once(
    "tests/test_static_vendor_root_fallback.py",
    '''    _assert_protected(item)\n    assert item.target_kind is CleanupTargetKind.DIRECTORY\n\n    with pytest.raises''',
    '''    _assert_protected(item)\n    assert item is not None\n    assert item.target_kind is CleanupTargetKind.DIRECTORY\n\n    with pytest.raises''',
)
append_anchor = "\ndef test_attached_audited_tool_rule_retains_vendor_directory_authority(tmp_path: Path) -> None:\n"
test_path = Path("tests/test_static_vendor_root_fallback.py")
test_text = test_path.read_text(encoding="utf-8")
if append_anchor not in test_text:
    raise RuntimeError("static vendor positive-test anchor missing")
file_test = r'''

def test_attached_whole_tree_rule_does_not_authorize_unmatched_individual_file(
    tmp_path: Path,
) -> None:
    rules = default_rules()
    root = tmp_path / "audited-vendor-root"
    root.mkdir()
    application_rule = next(
        rule for rule in CLAUDE_RULES if rule.allow_whole_tree and rule.owner.value == "TOOL"
    )
    known = KnownCleanupRoot(
        path=root,
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.VENDOR_MANAGED,
        label="Audited vendor root",
        application_rule=application_rule,
    )
    item = triage_file(
        _record(root / "unmatched.blob", directory=False),
        known_roots=(known,),
        delete_config=rules.delete.classification,
        keep_config=rules.keep.classification,
        now=_NOW,
        temp_root=tmp_path / "different-temp",
    )
    _assert_protected(item)
    assert "unmatched_file" in item.tags
'''
test_text = test_text.replace(append_anchor, file_test + append_anchor, 1)
test_path.write_text(test_text, encoding="utf-8", newline="\n")

replace_once(
    "docs/static-vendor-root-reaudit.md",
    "- runtime treats legacy/static `VENDOR_MANAGED` roots without an attached audited application rule as protected, so old sidecars fail closed;\n- deterministic vendor whole-tree authority requires an attached `ApplicationCleanupRule` whose owner is TOOL and whose audited policy explicitly allows whole-tree cleanup;",
    "- runtime treats legacy/static `VENDOR_MANAGED` roots without an attached audited application rule as protected, so old sidecars fail closed;\n- individual files never inherit authority from a vendor root: file mutation still requires the application classifier or a separately confirmed file-level rule;\n- deterministic vendor whole-tree authority requires an attached `ApplicationCleanupRule` whose owner is TOOL and whose audited policy explicitly allows whole-tree cleanup;",
)
