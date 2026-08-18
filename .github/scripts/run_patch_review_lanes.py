from __future__ import annotations

import re

from patch_review_lanes import main
import patch_review_lanes


def sub_once(text: str, pattern: str, replacement: str, *, flags: int = 0) -> str:
    compiled = re.compile(pattern, flags)
    updated, count = compiled.subn(lambda _match: replacement, text, count=1)
    if count != 1:
        raise RuntimeError(f"expected one match, got {count}: {pattern[:100]}")
    return updated


patch_review_lanes.sub_once = sub_once
main()
