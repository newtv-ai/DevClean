from __future__ import annotations

from pathlib import Path

import pytest

from devclean.core.user_rules import RuleDecision, add_ai_verdicts, add_user_verdicts, load_rules


def _environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVCLEAN_DATA_DIR", str(tmp_path / "DevClean-data"))
    monkeypatch.setenv("USERPROFILE", r"C:\Users\person")
    monkeypatch.setenv("APPDATA", r"C:\Users\person\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\person\AppData\Local")
    monkeypatch.setenv("TEMP", r"C:\Users\person\AppData\Local\Temp")
    monkeypatch.setenv("DEVCLEAN_YARN_CLASSIC_CACHE_DIR", r"E:\Yarn\Cache\v6")
    monkeypatch.setenv("DEVCLEAN_YARN_GLOBAL_FOLDER", r"F:\Yarn\Global")


def test_ai_cannot_reintroduce_raw_yarn_machine_cache_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"E:\Yarn\Cache\v6\npm-react\node_modules\react\index.js",
        r"F:\Yarn\Global\cache\react-npm-19.0.0.zip",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "old regenerable Yarn cache") for path in protected],
    )

    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_verdict_does_not_turn_protected_yarn_cache_into_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    path = r"F:\Yarn\Global\cache\react-npm-19.0.0.zip"
    updated = add_user_verdicts(
        load_rules(),
        [(path, RuleDecision.DELETE, "用户希望清理 Yarn cache")],
    )

    assert updated.decision_for(path) is None
