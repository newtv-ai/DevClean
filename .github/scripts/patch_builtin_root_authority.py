from __future__ import annotations

import json
from pathlib import Path


SCAN_RULES_PATH = Path("src/devclean/config/scan-rules.json")
RULE_TEST_PATH = Path("tests/_test_rules_impl.py")
POLICIES = {
    "system-crash-dumps": "AGE_BASED_REVIEW",
    "windows-maintenance": "MANUAL_REVIEW",
    "windows-update-downloads": "MANUAL_REVIEW",
    "windows-old": "MANUAL_REVIEW",
    "lmstudio": "MANUAL_REVIEW",
    "ide-working-caches": "MANUAL_REVIEW",
    "general-tool-caches": "MANUAL_REVIEW",
}


def main() -> None:
    data = json.loads(SCAN_RULES_PATH.read_text(encoding="utf-8"))
    roots = data["known_cleanup_roots"]
    by_id = {root["id"]: root for root in roots}
    missing = sorted(set(POLICIES) - set(by_id))
    if missing:
        raise RuntimeError(f"missing stock cleanup roots: {missing}")

    for rule_id, policy in POLICIES.items():
        by_id[rule_id]["policy"] = policy

    delete_root_ids = list(data["delete_root_ids"])
    data["delete_root_ids"] = [
        rule_id for rule_id in delete_root_ids if rule_id != "windows-old"
    ]
    SCAN_RULES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    tests = RULE_TEST_PATH.read_text(encoding="utf-8")
    old = '    assert rules.scan.delete_root_ids == {"windows-old"}\n'
    new = "    assert rules.scan.delete_root_ids == set()\n"
    if tests.count(old) != 1:
        raise RuntimeError("packaged rule authority assertion changed unexpectedly")
    RULE_TEST_PATH.write_text(tests.replace(old, new, 1), encoding="utf-8")


if __name__ == "__main__":
    main()
