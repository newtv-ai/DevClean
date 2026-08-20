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
    monkeypatch.setenv("NPM_CONFIG_PREFIX", r"D:\npm-global")
    monkeypatch.setenv("NPM_CONFIG_CACHE", r"E:\npm-cache")


def test_ai_cannot_delete_npm_config_lockfiles_or_global_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    protected = (
        r"C:\Users\person\.npmrc",
        r"C:\Users\person\src\app\package.json",
        r"C:\Users\person\src\app\package-lock.json",
        r"C:\Users\person\src\app\npm-shrinkwrap.json",
        r"D:\npm-global\node_modules\typescript\lib\tsc.js",
        r"D:\npm-global\tsc.cmd",
    )
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(path, RuleDecision.DELETE, "old or large file") for path in protected],
    )
    for path in protected:
        assert updated.decision_for(path) is None
    assert updated.ai_rule_count == before


def test_user_delete_of_npm_global_install_does_not_become_generic_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    package = r"D:\npm-global\node_modules\typescript\lib\tsc.js"
    updated = add_user_verdicts(
        load_rules(),
        [(package, RuleDecision.DELETE, "用户想移除全局工具")],
    )
    assert updated.decision_for(package) is None


def test_ai_cannot_reintroduce_raw_npm_cache_delete_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(tmp_path, monkeypatch)
    cache_file = r"E:\npm-cache\_cacache\content-v2\sha512\aa\blob"
    baseline = load_rules()
    before = baseline.ai_rule_count
    updated = add_ai_verdicts(
        baseline,
        [(cache_file, RuleDecision.DELETE, "regenerable npm cache")],
    )
    assert updated.decision_for(cache_file) is None
    assert updated.ai_rule_count == before
