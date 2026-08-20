from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one replacement in {path}, found {count}")
    path.write_text(text.replace(old, new), encoding="utf-8")


scan_path = ROOT / "src/devclean/config/scan-rules.json"
scan = json.loads(scan_path.read_text(encoding="utf-8"))
age_ids = {
    item["id"]
    for item in scan["known_cleanup_roots"]
    if item["policy"] == "AGE_BASED_REVIEW"
}
expected_age_ids = {"user-temp", "windows-temp", "user-crash-dumps"}
if age_ids != expected_age_ids:
    raise RuntimeError(f"unexpected AGE_BASED_REVIEW roots: {sorted(age_ids)}")
for item in scan["known_cleanup_roots"]:
    if item["id"] in expected_age_ids:
        item["policy"] = "REPORT_ONLY"
scan_path.write_text(json.dumps(scan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

triage_path = ROOT / "src/devclean/core/triage.py"

replace_once(
    triage_path,
    '''    elif scope is DirectoryScope.AGED_TEMP_ITEM:\n        category = CleanupCategory.USER_TEMP\n        recovery = RecoveryCapability.VENDOR_REDOWNLOAD_BEST_EFFORT\n        lane = ReviewLane.DETERMINISTIC_CANDIDATE\n        risk_tier = RiskTier.LOW\n        evidence_kind = EvidenceKind.AGE_AND_APPROVED_ROOT\n        actionability = Actionability.REVIEW_PLAN\n        execution_policy = ExecutionPolicy.USER_CHOICE_DELETE\n        reason = f"{path.name}：临时目录中超过 {delete_config.old_temp_days} 天未改动的整个条目"\n        tags = ("whole_directory", "aged_temp_item", "tool_decision")\n''',
    '''    elif scope is DirectoryScope.AGED_TEMP_ITEM:\n        category = CleanupCategory.USER_TEMP\n        recovery = RecoveryCapability.UNKNOWN\n        lane = ReviewLane.REPORT_ONLY\n        risk_tier = RiskTier.PROTECTED\n        evidence_kind = EvidenceKind.FILESYSTEM_OBSERVATION\n        actionability = Actionability.REPORT_ONLY\n        execution_policy = ExecutionPolicy.NONE\n        reason = f"{path.name}：文件时间只能说明观察到的年龄，不能证明整个临时目录当前可删"\n        tags = ("whole_directory", "aged_temp_item", "report_only")\n''',
)

replace_once(
    triage_path,
    '''        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:\n            if _is_older_than(\n                last_write_time_ns,\n                timedelta(days=delete_config.old_temp_days),\n                now,\n            ):\n                return _Classification(\n                    known.category,\n                    ReviewLane.DETERMINISTIC_CANDIDATE,\n                    RiskTier.LOW,\n                    EvidenceKind.AGE_AND_APPROVED_ROOT,\n                    Actionability.REVIEW_PLAN,\n                    ExecutionPolicy.USER_CHOICE_DELETE,\n                    RecoveryCapability.UNKNOWN,\n                    (\n                        f"{known.label}：已知根目录且超过 "\n                        f"{delete_config.old_temp_days} 天，判定可以删除；执行仍需你确认"\n                    ),\n                    ("known_root", "older_than_configured_days"),\n                )\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.KNOWN_ROOT_HEURISTIC,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.UNKNOWN,\n                (\n                    f"{known.label}：属于已知临时目录，但未达到 "\n                    f"{delete_config.old_temp_days} 天阈值；工具直接保留，不要求用户判断"\n                ),\n                ("known_root", "recent", "report_only"),\n            )\n''',
    '''        if known.policy is CleanupPolicy.AGE_BASED_REVIEW:\n            return _Classification(\n                known.category,\n                ReviewLane.REPORT_ONLY,\n                RiskTier.PROTECTED,\n                EvidenceKind.FILESYSTEM_OBSERVATION,\n                Actionability.REPORT_ONLY,\n                ExecutionPolicy.NONE,\n                RecoveryCapability.UNKNOWN,\n                (\n                    f"{known.label}：旧规则曾按文件时间授予删除权限；"\n                    "当前没有来源证明单靠 mtime 能代表未使用或安全删除，工具直接保护"\n                ),\n                ("known_root", "legacy_age_based_review", "report_only"),\n            )\n''',
)

replace_once(
    triage_path,
    '''    root = temp_root or Path(tempfile.gettempdir())\n    if _is_descendant(path, root) and _is_older_than(\n        last_write_time_ns,\n        timedelta(days=delete_config.old_temp_days),\n        now,\n    ):\n        return _Classification(\n            CleanupCategory.USER_TEMP,\n            ReviewLane.DETERMINISTIC_CANDIDATE,\n            RiskTier.LOW,\n            EvidenceKind.AGE_AND_APPROVED_ROOT,\n            Actionability.REVIEW_PLAN,\n            ExecutionPolicy.USER_CHOICE_DELETE,\n            RecoveryCapability.UNKNOWN,\n            (\n                f"当前用户临时目录中超过 {delete_config.old_temp_days} 天，"\n                "判定可以删除；执行仍需你确认"\n            ),\n            ("older_than_configured_days",),\n        )\n''',
    '''    root = temp_root or Path(tempfile.gettempdir())\n    if _is_descendant(path, root):\n        return _Classification(\n            CleanupCategory.USER_TEMP,\n            ReviewLane.REPORT_ONLY,\n            RiskTier.PROTECTED,\n            EvidenceKind.FILESYSTEM_OBSERVATION,\n            Actionability.REPORT_ONLY,\n            ExecutionPolicy.NONE,\n            RecoveryCapability.UNKNOWN,\n            (\n                "位于当前用户临时目录，但 Windows 的维护语义是清理未在使用的临时文件；"\n                "单独的 mtime/年龄不能证明当前未使用，工具直接保护"\n            ),\n            ("temp_root", "unknown", "report_only"),\n        )\n''',
)

replace_once(
    triage_path,
    '''def _is_aged_temp_child(\n    path: Path,\n    known_roots: tuple[KnownCleanupRoot, ...],\n    old_temp_days: int,\n) -> bool:\n    parent = _normalized_path(path.parent)\n    if parent not in _age_based_known_roots(known_roots):\n        return False\n    try:\n        modified = path.stat().st_mtime\n    except OSError:\n        return False\n    return (datetime.now(UTC).timestamp() - modified) > timedelta(\n        days=old_temp_days\n    ).total_seconds()\n\n\n@lru_cache(maxsize=8)\ndef _age_based_known_roots(\n    known_roots: tuple[KnownCleanupRoot, ...],\n) -> frozenset[str]:\n    return frozenset(\n        _normalized_path(root.path)\n        for root in known_roots\n        if root.policy is CleanupPolicy.AGE_BASED_REVIEW\n    )\n\n\n''',
    '',
)

replace_once(
    triage_path,
    '''    if not is_known_root and not is_tool_output:\n        if _is_aged_temp_child(path, known_roots, delete_config.old_temp_days):\n            return DirectoryScope.AGED_TEMP_ITEM\n        if is_stale_version_directory(path, delete_config):\n            return DirectoryScope.STALE_VERSION\n        return DirectoryScope.NOT_ELIGIBLE\n''',
    '''    if not is_known_root and not is_tool_output:\n        if is_stale_version_directory(path, delete_config):\n            return DirectoryScope.STALE_VERSION\n        return DirectoryScope.NOT_ELIGIBLE\n''',
)

test_path = ROOT / "tests/test_generic_review_routing.py"
replace_once(
    test_path,
    '''def test_recent_age_based_root_is_kept_without_asking_user(tmp_path: Path) -> None:\n    root = tmp_path / "known-temp"\n    root.mkdir()\n    known = KnownCleanupRoot(\n        path=root,\n        category=CleanupCategory.USER_TEMP,\n        policy=CleanupPolicy.AGE_BASED_REVIEW,\n        label="Known temp",\n    )\n    recent = root / "recent.tmp"\n    old = root / "old.tmp"\n\n    _assert_protected(_triage(recent, age_days=0, known_roots=(known,)))\n    old_item = _triage(old, age_days=3, known_roots=(known,))\n    assert old_item.lane is ReviewLane.DETERMINISTIC_CANDIDATE\n    assert app.is_direct_cleanup_eligible(old_item)\n''',
    '''def test_legacy_age_based_root_is_protected_regardless_of_mtime(tmp_path: Path) -> None:\n    root = tmp_path / "known-temp"\n    root.mkdir()\n    known = KnownCleanupRoot(\n        path=root,\n        category=CleanupCategory.USER_TEMP,\n        policy=CleanupPolicy.AGE_BASED_REVIEW,\n        label="Known temp",\n    )\n    recent = root / "recent.tmp"\n    old = root / "old.tmp"\n\n    _assert_protected(_triage(recent, age_days=0, known_roots=(known,)))\n    _assert_protected(_triage(old, age_days=365, known_roots=(known,)))\n''',
)

append = '''\n\ndef test_explicit_temp_root_never_gains_raw_age_authority(tmp_path: Path) -> None:\n    rules = default_rules()\n    temp_root = tmp_path / "Temp"\n    temp_root.mkdir()\n    path = temp_root / "very-old.tmp"\n    item = triage_file(\n        _file_record(path, age_days=365),\n        delete_config=rules.delete.classification,\n        keep_config=rules.keep.classification,\n        now=_NOW,\n        temp_root=temp_root,\n    )\n    _assert_protected(item)\n    assert "temp_root" in item.tags\n\n\ndef test_legacy_age_root_child_directory_is_not_whole_tree_candidate(tmp_path: Path) -> None:\n    rules = default_rules()\n    root = tmp_path / "Temp"\n    child = root / "old-session"\n    child.mkdir(parents=True)\n    known = KnownCleanupRoot(\n        path=root,\n        category=CleanupCategory.USER_TEMP,\n        policy=CleanupPolicy.AGE_BASED_REVIEW,\n        label="Legacy temp",\n    )\n    item = triage_directory(\n        _directory_record(child, root=root),\n        delete_config=rules.delete.classification,\n        keep_config=rules.keep.classification,\n        known_roots=(known,),\n    )\n    assert item is None\n\n\ndef test_packaged_scan_roots_have_no_age_based_authority() -> None:\n    rules = default_rules()\n    assert not any(\n        CleanupPolicy(root.policy) is CleanupPolicy.AGE_BASED_REVIEW\n        for root in rules.scan.known_cleanup_roots\n    )\n'''
text = test_path.read_text(encoding="utf-8")
if "test_explicit_temp_root_never_gains_raw_age_authority" not in text:
    test_path.write_text(text + append, encoding="utf-8")

tracker_path = ROOT / "docs/full-rule-reaudit-2026-08.md"
tracker = tracker_path.read_text(encoding="utf-8")
tracker = tracker.replace(
    "| AGE_BASED_REVIEW temp lifecycle | ⏳ next | Microsoft Storage Sense semantics do not justify raw one-day mtime authority; rework this next |",
    "| AGE_BASED_REVIEW temp lifecycle | ✅ phase 4 | raw mtime/age authority removed; legacy AGE roots fail closed and packaged temp/crash roots are discovery-only |",
)
tracker = tracker.replace(
    "| `user-temp` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |",
    "| `user-temp` | `REPORT_ONLY` | phase 4: Storage Sense semantics do not justify raw mtime deletion authority |",
)
tracker = tracker.replace(
    "| `windows-temp` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |",
    "| `windows-temp` | `REPORT_ONLY` | phase 4: Windows temp is discovery-only unless a narrower source-owned lane applies |",
)
tracker = tracker.replace(
    "| `user-crash-dumps` | `AGE_BASED_REVIEW` | phase 2 generic boundary applied; vendor/source detail still tracked separately |",
    "| `user-crash-dumps` | `REPORT_ONLY` | phase 4: generic age removed; exact Windows crash-dump USER_REVIEW lane remains source-specific |",
)
# Synchronise the packaged-policy column with the now discovery-only scan config.
policy_by_id = {item["id"]: item["policy"] for item in scan["known_cleanup_roots"]}
for rule_id, policy in policy_by_id.items():
    tracker = re.sub(
        rf"(\| `{re.escape(rule_id)}` \| `)[A-Z_]+(` \|)",
        rf"\g<1>{policy}\2",
        tracker,
    )
tracker_path.write_text(tracker, encoding="utf-8")

age_doc = '''# AGE_BASED_REVIEW lifecycle re-audit\n\nAudited: 2026-08-20\n\n## Source conclusion\n\nMicrosoft documents Storage Sense temporary-file cleanup as removal of the user's temporary files that **aren't in use**. Its separately configurable day thresholds apply to Downloads, Recycle Bin and cloud-content dehydration; Microsoft does not document a generic rule that an arbitrary file under `%TEMP%`, `%SYSTEMROOT%\\Temp`, `%SYSTEMROOT%\\SystemTemp` or `%LOCALAPPDATA%\\CrashDumps` becomes safe to raw-delete after one day of mtime age.\n\nDevClean therefore treats age and mtime as benefit/observation evidence only. They no longer create mutation authority.\n\n## Product correction\n\n- packaged `user-temp`, `windows-temp` and `user-crash-dumps` roots are REPORT_ONLY discovery anchors;\n- old sidecars that still say `AGE_BASED_REVIEW` fail closed at runtime regardless of age;\n- the implicit current-user temp-root fallback is REPORT_ONLY even for very old files;\n- an old child directory under a legacy age root no longer becomes a whole-tree candidate;\n- the unreachable `AGED_TEMP_ITEM` triage branch is also protected as a defense-in-depth ceiling;\n- separately source-audited application rules still run before generic root policy and are unchanged;\n- exact learned/common **file** knowledge may still supplement a generic temp-file uncertainty when it passes the existing file-only authority boundary, but no learned rule gains directory authority.\n\n## Why not change 1 day to 7/30 days?\n\nA larger number would still confuse age with lifecycle. A stale mtime does not prove that a file is closed, unused, reconstructable, unreferenced, or outside an application's recovery/rollback state. Where Windows or an application exposes an exact maintenance operation, DevClean should use that source-owned lane instead of inventing a raw-age contract.\n\n## Windows crash dumps\n\nThe generic `%LOCALAPPDATA%\\CrashDumps` scan root is now discovery-only. DevClean's dedicated Windows crash-dump inventory remains the correct lane: exact dump objects have known diagnostic meaning and are USER_REVIEW rather than being auto-authorized by age. WER queue/archive stores remain protected.\n'''
(ROOT / "docs/age-based-review-reaudit.md").write_text(age_doc, encoding="utf-8")

status_path = ROOT / "docs/storage-audit-status.md"
status = status_path.read_text(encoding="utf-8")
needle = "| Windows crash dumps | exact CrashControl large/small, LiveKernelReports root/component, and WER LocalDumps `.dmp` files USER_REVIEW with handle-bound exact deletion; WER queue/archive report stores REPORT_ONLY |"
addition = needle + "\n| Generic Windows temp/age roots | REPORT_ONLY discovery; raw mtime/age never creates deletion authority, while narrower source-owned application/Windows lanes remain eligible |"
if needle in status and "| Generic Windows temp/age roots |" not in status:
    status = status.replace(needle, addition)
status_path.write_text(status, encoding="utf-8")
