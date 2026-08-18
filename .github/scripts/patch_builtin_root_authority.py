from __future__ import annotations

import json
from pathlib import Path


PATH = Path("src/devclean/config/scan-rules.json")
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
    data = json.loads(PATH.read_text(encoding="utf-8"))
    roots = data["known_cleanup_roots"]
    by_id = {root["id"]: root for root in roots}
    missing = sorted(set(POLICIES) - set(by_id))
    if missing:
        raise RuntimeError(f"missing stock cleanup roots: {missing}")

    for rule_id, policy in POLICIES.items():
        by_id[rule_id]["policy"] = policy

    delete_root_ids = list(data["delete_root_ids"])
    data["delete_root_ids"] = [rule_id for rule_id in delete_root_ids if rule_id != "windows-old"]

    PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
