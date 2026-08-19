from __future__ import annotations

import devclean.core.jetbrains_leftover_maintenance as leftovers


def test_leftover_selector_matches_only_audited_modern_default_layouts() -> None:
    assert leftovers._supported_selector("IntelliJIdea2020.1")
    assert leftovers._supported_selector("Rider2026.2")
    assert not leftovers._supported_selector("IntelliJIdea2019.3")
    assert not leftovers._supported_selector("AndroidStudio2026.2")
    assert not leftovers._supported_selector("SomeOtherTool2026.2")
